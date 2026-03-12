#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高级 IPTV 频道列表获取脚本 (支持组播、酒店、秒播源) - Python 重构版
Version: 3.0.2-py
Date: 2026-03-10
License: MIT
"""

import os
import sys
import re
import json
import time
import hashlib
import logging
import argparse
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import BoundedSemaphore
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter, Retry
from bs4 import BeautifulSoup

# =============================================================================
# 配置类 (从 JSON 文件加载)
# =============================================================================
DEFAULT_CONFIG = {
    "main_site": "https://tonkiang.us",
    "backup_site": "http://foodieguide.com/iptvsearch",
    "request_timeout": 15,
    "retry_count": 4,
    "base_sleep": 1,
    "max_concurrent_total": 5,
    "max_concurrent_per_host": 2,
    "user_agents": [
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64; rv:122.0) Gecko/20100101 Firefox/122.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
    ],
    "dir_response": "response_files",
    "dir_multicast": "multicastList",
    "dir_hotel": "hotelList",
    "dir_mqlive": "mqliveList",
    "dir_state": "state_files",
    "log_file": "iptv_fetch.log",
    "stats_json": "stats_report.json"
}

class Config:
    """加载和保存配置"""
    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.data = DEFAULT_CONFIG.copy()
        self._load()

    def _load(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    user_config = json.load(f)
                    self.data.update(user_config)
                logging.info(f"已加载配置文件: {self.config_file}")
            except Exception as e:
                logging.warning(f"加载配置文件失败: {e}，使用默认配置")
        else:
            logging.info("配置文件不存在，使用默认配置")
            self._save_default()

    def _save_default(self):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
            logging.info(f"已生成默认配置文件: {self.config_file}")
        except Exception as e:
            logging.error(f"无法保存配置文件: {e}")

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)


# =============================================================================
# 日志设置 (简洁美观，支持颜色)
# =============================================================================
def setup_logger(debug_mode: bool = False, log_file: Optional[str] = None):
    """配置日志记录器，控制台带颜色，文件无颜色"""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if debug_mode else logging.INFO)

    # 移除已有的处理器
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # 控制台处理器（带颜色）
    console_handler = logging.StreamHandler(sys.stdout)
    console_format = '%(asctime)s [%(levelname)s] %(message)s'
    try:
        from colorlog import ColoredFormatter
        console_format = '%(log_color)s%(asctime)s [%(levelname)s] %(message)s%(reset)s'
        formatter = ColoredFormatter(
            console_format,
            datefmt='%Y-%m-%d %H:%M:%S',
            reset=True,
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'red,bg_white',
            }
        )
    except ImportError:
        formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件处理器（无颜色）
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_format = '%(asctime)s [%(levelname)s] %(message)s'
        file_formatter = logging.Formatter(file_format, datefmt='%Y-%m-%d %H:%M:%S')
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

    return logger


# =============================================================================
# 工具函数
# =============================================================================
def url_encode(s: str) -> str:
    """URL 编码"""
    return urllib.parse.quote(s, safe='')

def get_file_hash(file_path: str) -> str:
    """计算文件的 MD5 哈希值"""
    if not os.path.isfile(file_path):
        return ""
    hash_md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def parse_indices(input_str: str, total: int) -> List[int]:
    """
    解析序号选择器，支持格式：
    - 单个: 1,2,3
    - 范围: 1-5
    - 组合: 1,3-5,7
    - 关键字: all
    返回有效序号的列表（1-based）
    """
    if not input_str:
        return []
    if input_str.lower() == 'all':
        return list(range(1, total + 1))

    indices = set()
    parts = input_str.split(',')
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                start, end = map(int, part.split('-'))
                for i in range(start, end + 1):
                    if 1 <= i <= total:
                        indices.add(i)
            except ValueError:
                logging.warning(f"忽略无效序号范围: {part}")
        else:
            try:
                i = int(part)
                if 1 <= i <= total:
                    indices.add(i)
                else:
                    logging.warning(f"忽略超出范围的序号: {i}")
            except ValueError:
                logging.warning(f"忽略无效序号: {part}")
    return sorted(indices)

def ensure_dir(path: str):
    """确保目录存在"""
    Path(path).mkdir(parents=True, exist_ok=True)


# =============================================================================
# 省份数据 (索引从1开始)
# =============================================================================
PROVINCES = {
    1: ("河北", "hebei"),
    2: ("山西", "shanxi"),
    3: ("辽宁", "liaoning"),
    4: ("吉林", "jilin"),
    5: ("黑龙江", "heilongjiang"),
    6: ("江苏", "jiangsu"),
    7: ("浙江", "zhejiang"),
    8: ("安徽", "anhui"),
    9: ("福建", "fujian"),
    10: ("江西", "jiangxi"),
    11: ("山东", "shandong"),
    12: ("河南", "henan"),
    13: ("湖北", "hubei"),
    14: ("湖南", "hunan"),
    15: ("广东", "guangdong"),
    16: ("海南", "hainan"),
    17: ("四川", "sichuan"),
    18: ("贵州", "guizhou"),
    19: ("云南", "yunnan"),
    20: ("陕西", "shaanxi"),
    21: ("甘肃", "gansu"),
    22: ("青海", "qinghai"),
    23: ("台湾", "taiwan"),
    24: ("内蒙古", "neimenggu"),
    25: ("广西", "guangxi"),
    26: ("西藏", "xizang"),
    27: ("宁夏", "ningxia"),
    28: ("新疆", "xinjiang"),
    29: ("北京", "beijing"),
    30: ("天津", "tianjin"),
    31: ("上海", "shanghai"),
    32: ("重庆", "chongqing"),
    33: ("香港", "xianggang"),
    34: ("澳门", "aomen"),
}

OPERATORS = ["联通", "电信", "移动"]


# =============================================================================
# IPTV 获取器主类
# =============================================================================
class IPTVFetcher:
    def __init__(self, config: Config, debug: bool = False):
        self.config = config
        self.debug = debug

        # 创建所需目录
        for d in [config['dir_response'], config['dir_multicast'],
                  config['dir_hotel'], config['dir_mqlive'], config['dir_state']]:
            ensure_dir(d)

        # 站点相关
        self.main_site = config['main_site']
        self.backup_site = config['backup_site']
        self.active_site: Optional[str] = None
        self.tk: Optional[str] = None
        self.code: Optional[str] = None

        # 统计信息
        self.start_time = time.time()
        self.total_success = 0
        self.total_failed = 0
        self.total_skipped = 0

        # 会话管理（带重试和连接池）
        self.session = self._create_session()

        # 并发控制信号量（限制总并发数）
        self.semaphore = BoundedSemaphore(config['max_concurrent_total'])

        # 日志记录
        self.logger = logging.getLogger()

    def _create_session(self) -> requests.Session:
        """创建带重试和连接池的会话"""
        session = requests.Session()

        # 配置重试策略
        retries = Retry(
            total=self.config['retry_count'],
            backoff_factor=self.config['base_sleep'],
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(
            max_retries=retries,
            pool_connections=10,
            pool_maxsize=self.config['max_concurrent_total']
        )
        session.mount('http://', adapter)
        session.mount('https://', adapter)

        # 设置默认 headers
        session.headers.update({
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        })

        return session

    def _get_random_ua(self) -> str:
        """随机选择一个 User-Agent"""
        agents = self.config['user_agents']
        return agents[int(time.time() * 1000) % len(agents)]

    def _request(self, url: str, output_file: Optional[str] = None, **kwargs) -> Optional[str]:
        """
        发送 GET 请求，支持重试、超时、随机 UA。
        如果指定 output_file，则将内容写入文件并返回文件路径；
        否则返回响应文本。
        """
        headers = kwargs.pop('headers', {})
        if 'User-Agent' not in headers:
            headers['User-Agent'] = self._get_random_ua()

        timeout = kwargs.pop('timeout', self.config['request_timeout'])

        try:
            # 使用信号量控制总并发数
            with self.semaphore:
                self.logger.debug(f"请求 URL: {url}")
                resp = self.session.get(url, headers=headers, timeout=timeout, **kwargs)
                resp.raise_for_status()
                content = resp.text

            if output_file:
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(content)
                self.logger.debug(f"已保存到: {output_file}")
                return output_file
            else:
                return content
        except Exception as e:
            self.logger.error(f"请求失败 {url}: {e}")
            return None

    def authenticate(self) -> bool:
        """尝试主站和备用站，获取 tk 和 code"""
        for site, name in [(self.main_site, "tonkiang"), (self.backup_site, "foodguide")]:
            self.logger.info(f"正在从 [{name}] 获取认证参数...")
            url = f"{site}/iptvmulticast.php"
            out_file = os.path.join(self.config['dir_response'], f"auth_{name}.html")
            result = self._request(url, out_file)
            if not result:
                continue

            # 解析 tk 和 code
            with open(out_file, 'r', encoding='utf-8') as f:
                html = f.read()

            # 提取 tk
            match = re.search(r'channellist\.html\?ip=[^&]+&tk=([^"&\s]+)', html)
            if match:
                self.tk = match.group(1)
            else:
                continue

            # 提取 code - 修正为正则匹配直到遇到双引号或单引号
            code_match = re.search(r'code=([^"\']*)', html)
            self.code = code_match.group(1) if code_match else ""

            self.active_site = site
            self.logger.info(f"认证成功: tk={self.tk[:8]}..., code={self.code}, 当前站点: {self.active_site}")
            return True

        self.logger.error("所有站点认证失败")
        return False

    def fetch_hosts(self, source_type: str, query_param: str = "") -> Optional[str]:
        """
        获取指定类型的主机列表，返回主机列表文件路径（如 multicastHost.txt）
        支持增量更新（比较第一页的哈希值）
        """
        # 确定基础 URL 和参数 p 的值
        if source_type == "multicast":
            url_base = f"{self.active_site}/iptvmulticast.php"
            p_val = "2"
        elif source_type == "hotel":
            if self.active_site == "https://tonkiang.us":
                url_base = f"{self.active_site}/iptvhotelx.php"
            else:
                url_base = f"{self.active_site}/iptvhotel.php"
            p_val = "3"
        elif source_type == "mqlive":
            url_base = f"{self.active_site}/mqlive.php"
            p_val = "1"
        else:
            raise ValueError(f"无效的来源类型: {source_type}")

        # 主机列表文件名（旧文件）
        host_file = f"{source_type}Host.txt"
        state_file = os.path.join(self.config['dir_state'], f"{source_type}_{abs(hash(query_param))}.json")

        # 构建检查页面的 URL（第一页）
        check_url = f"{url_base}?page=1&iphone16={url_encode(query_param)}&code={self.code}"
        check_file = os.path.join(self.config['dir_response'], f"check_{source_type}.html")

        # 增量更新检查
        if os.path.exists(host_file) and os.path.exists(state_file):
            if self._request(check_url, check_file):
                current_hash = get_file_hash(check_file)
                try:
                    with open(state_file, 'r', encoding='utf-8') as f:
                        state = json.load(f)
                    last_hash = state.get('last_page1_hash', '')
                    if current_hash == last_hash and last_hash:
                        self.logger.info(f"[{source_type}] 数据未变化，跳过下载，复用旧列表")
                        # 更新时间戳
                        state['last_update'] = int(time.time())
                        with open(state_file, 'w', encoding='utf-8') as f:
                            json.dump(state, f, indent=2)
                        return host_file
                except Exception:
                    pass

        # 需要获取新数据
        temp_file = host_file + ".tmp"
        page_success = 0

        with open(temp_file, 'w', encoding='utf-8') as out:
            for page in range(1, 6):  # 最多 5 页
                url = f"{url_base}?page={page}&iphone16={url_encode(query_param)}&code={self.code}"
                resp_file = os.path.join(self.config['dir_response'], f"hosts_{source_type}_p{page}.html")
                if not self._request(url, resp_file):
                    self.logger.warning(f"获取第 {page} 页失败，跳过")
                    continue
                page_success += 1

                # 解析主机信息
                with open(resp_file, 'r', encoding='utf-8') as f:
                    html = f.read()
                hosts = self._parse_hosts_page(html, source_type)
                for line in hosts:
                    out.write(line + "\n")

                time.sleep(0.5)  # 礼貌性延迟

        if page_success == 0:
            self.logger.error("所有页面获取失败")
            return None

        # 处理结果：去重、添加注释头
        valid_hosts = self._dedup_hosts(temp_file)
        final_hosts = []
        final_hosts.append(f"# {source_type} 主机信息 (生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
        final_hosts.append("# 格式：IP|端口/数量 | 上线时间 | 状态 | 类型 | 运营商")
        final_hosts.append("# 注意：已自动过滤 '暂时失效' 的主机")
        final_hosts.extend(valid_hosts)

        with open(host_file, 'w', encoding='utf-8') as f:
            f.write("\n".join(final_hosts))

        count = len(valid_hosts)
        self.logger.info(f"[{source_type}] 成功提取 {count} 个有效主机")

        # 更新状态文件
        state = {
            'source_type': source_type,
            'query_param': query_param,
            'last_update': int(time.time()),
            'last_page1_hash': get_file_hash(check_file),
            'total_hosts': count
        }
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)

        os.remove(temp_file)
        return host_file

    def _parse_hosts_page(self, html: str, source_type: str) -> List[str]:
        """
        解析主机列表页面，返回每行主机信息字符串。
        格式：IP|端口/数量 | 上线时间 | 状态 | 类型 | 运营商
        """
        soup = BeautifulSoup(html, 'html.parser')
        results = []
        for div in soup.find_all('div', class_='result'):
            # 跳过包含“暂时失效”的条目
            if '暂时失效' in div.get_text():
                continue

            # 提取 IP
            ip_link = div.find('a', href=re.compile(r'channellist\.html\?ip='))
            if not ip_link:
                continue
            ip = re.search(r'ip=([^&]+)', ip_link['href']).group(1)

            # 提取频道数（端口/数量）
            count_span = div.find('span', style="font-size: 18px;")
            count = count_span.get_text(strip=True) if count_span else "0"

            # 提取状态（存活天数等）
            status_div = div.find('div', style=re.compile(r'color:limegreen'))
            status = "未知"
            if status_div:
                status_text = status_div.get_text(strip=True)
                if '新上线' in status_text:
                    status = "新上线"
                elif '存活' in status_text:
                    days = re.search(r'存活\s*(\d+)\s*天', status_text)
                    status = f"存活{days.group(1)}天" if days else "存活"

            # 提取详细信息的 <i> 标签
            i_tag = div.find('i')
            online_time = ""
            types = ""
            isp = ""
            if i_tag:
                i_text = i_tag.get_text(strip=True)
                # 格式示例: "2026-03-03 17:40上线 北京北京市秒播 北京联通"
                match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2})上线\s*(.*)', i_text)
                if match:
                    online_time = match.group(1)
                    remainder = match.group(2)
                    # 拆分类型和运营商
                    parts = remainder.split()
                    if parts:
                        types = parts[0] if len(parts) > 0 else ""
                        isp = parts[1] if len(parts) > 1 else ""

            line = f"{ip}|{count}|{online_time}|{status}|{types}|{isp}"
            results.append(line)
        return results

    def _dedup_hosts(self, temp_file: str) -> List[str]:
        """对临时文件中的主机行去重（基于IP）"""
        seen = set()
        unique = []
        with open(temp_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ip = line.split('|')[0]
                if ip not in seen:
                    seen.add(ip)
                    unique.append(line)
        return unique

    def fetch_channels(self, host_line: str, source_type: str) -> Optional[str]:
        """
        为单个主机获取频道列表，保存到对应目录，并将第一个频道 URL 追加到 host.list。
        成功时返回主机行（用于记录成功），否则返回 None。
        """
        parts = host_line.split('|')
        if len(parts) < 1:
            return None
        host = parts[0]

        # 确定 p 值
        p_map = {"multicast": "2", "hotel": "1", "mqlive": "3"}
        p_val = p_map.get(source_type, "2")

        # 选择 API 路径
        if self.active_site == "https://tonkiang.us":
            api_path = "/getall26.php"
        else:
            api_path = "/getall.php"

        url = f"{self.active_site}{api_path}?ip={host}&c=&tk={self.tk}&p={p_val}"
        safe_host = host.replace('/', '_')
        resp_file = os.path.join(self.config['dir_response'], f"channels_{source_type}_{safe_host}.html")

        if not self._request(url, resp_file):
            return None

        # 检查返回内容有效性
        try:
            with open(resp_file, 'r', encoding='utf-8') as f:
                html = f.read()
        except:
            return None

        if '<div class="channel"' not in html or '暂时失效' in html:
            self.logger.debug(f"主机 {host} 返回内容无效或失效")
            return None

        # 提取运营商
        op_match = re.search(r'来自<b>([^<]+)</b>', html)
        operator = op_match.group(1) if op_match else "Unknown"

        # 提取频道数
        count_match = re.search(r'共有<b>(\d+)</b>', html)
        channel_count = count_match.group(1) if count_match else "0"

        # 解析频道列表
        soup = BeautifulSoup(html, 'html.parser')
        channels = []
        for result in soup.find_all('div', class_='result'):
            tip = result.find('div', class_='tip')
            if not tip:
                continue
            name = tip.get_text(strip=True)
            # 寻找 m3u8 链接
            m3u8_div = result.find_next('div', class_='m3u8')
            if m3u8_div:
                # 链接在 onclick 或文本中
                onclick_attr = m3u8_div.find('img', onclick=True)
                if onclick_attr and 'copyto' in onclick_attr['onclick']:
                    url_match = re.search(r"copyto\('([^']+)'\)", onclick_attr['onclick'])
                    if url_match:
                        channel_url = url_match.group(1)
                        channels.append((name, channel_url))
                else:
                    # 备选：从表格文本中提取
                    td = m3u8_div.find('td', style=lambda v: v and 'padding-left' in v)
                    if td:
                        url_text = td.get_text(strip=True)
                        if url_text.startswith('http'):
                            channels.append((name, url_text))

        if not channels:
            return None

        # 保存到文件
        safe_op = re.sub(r'[\\/*?:"<>|]', '', operator)
        filename = f"{safe_op}_{source_type}_{safe_host}_{channel_count}.txt"
        out_dir = self.config[f"dir_{source_type}"]  # 修正：使用字典访问
        final_path = os.path.join(out_dir, filename)

        with open(final_path, 'w', encoding='utf-8') as f:
            f.write(f"# {source_type} 频道信息 | 主机：{host} | 运营商：{operator}\n")
            f.write(f"# 频道数：{channel_count} | 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("# ----------------------------------------\n")
            for name, url in channels:
                f.write(f"{name},{url}\n")

        # 将第一个有效频道追加到 host.list
        test_host_file = os.path.join(out_dir, "host.list")
        first_line = f"{channels[0][0]},{channels[0][1]}"
        # 使用追加写入，由于 Python 多线程中文件写入是原子的（对于小数据），可以不加锁
        with open(test_host_file, 'a', encoding='utf-8') as f:
            f.write(first_line + "\n")

        self.logger.debug(f"已保存频道列表: {final_path} ({channel_count} 个频道)")
        return host_line  # 返回主机行表示成功

    def process_source_type(self, source_type: str, query_param: str = ""):
        """
        处理一种来源类型：获取主机列表 -> 合并去重 -> 并发获取频道列表 -> 更新主机列表
        """
        self.logger.info(f"===== 开始处理来源类型: [{source_type}] 查询: '{query_param}' =====")

        # 旧主机列表文件
        old_host_file = f"{source_type}Host.txt"
        new_host_file = f"{source_type}Host_new.tmp"
        merged_host_file = f"{source_type}Host_merged.tmp"
        host_lines_file = f"{source_type}Host_lines.tmp"
        success_hosts_tmp = f"{source_type}Host_success.tmp"

        # 备份旧列表
        backup_file = old_host_file + ".bak"
        if os.path.exists(old_host_file):
            import shutil
            shutil.copy2(old_host_file, backup_file)

        # 获取新主机列表（会覆盖 old_host_file）
        result = self.fetch_hosts(source_type, query_param)
        if not result:
            self.logger.error(f"获取 {source_type} 主机列表失败，跳过此类型")
            return

        # 将新获取的文件重命名为 new_host_file
        if os.path.exists(old_host_file):
            os.rename(old_host_file, new_host_file)
        else:
            # 理论上 fetch_hosts 成功后应该生成了文件，但为安全起见
            with open(new_host_file, 'w') as f:
                pass

        # 统计新主机数量（排除注释行）
        new_host_count = 0
        if os.path.exists(new_host_file):
            with open(new_host_file, 'r', encoding='utf-8') as f:
                new_host_count = sum(1 for line in f if line.strip() and not line.startswith('#'))

        # 统计旧主机数量（从备份文件）
        old_host_count = 0
        if os.path.exists(backup_file):
            with open(backup_file, 'r', encoding='utf-8') as f:
                old_host_count = sum(1 for line in f if line.strip() and not line.startswith('#'))

        # 合并新旧主机列表（新文件优先，IP 去重）
        with open(merged_host_file, 'w', encoding='utf-8') as out:
            out.write(f"# {source_type} 主机信息 (生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\n")
            out.write("# 格式：IP|端口/数量 | 上线时间 | 状态 | 类型 | 运营商\n")
            out.write("# 注意：已自动过滤 '暂时失效' 的主机\n")
            seen = set()
            # 先处理新文件
            if os.path.exists(new_host_file):
                with open(new_host_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        ip = line.split('|')[0]
                        if ip not in seen:
                            seen.add(ip)
                            out.write(line + "\n")
            # 再处理备份旧文件
            if os.path.exists(backup_file):
                with open(backup_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        ip = line.split('|')[0]
                        if ip not in seen:
                            seen.add(ip)
                            out.write(line + "\n")

        # 提取待测试主机列表（纯主机行）
        with open(host_lines_file, 'w', encoding='utf-8') as out:
            with open(merged_host_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not line.startswith('#'):
                        out.write(line)

        # 统计总待测试主机数
        total_hosts = 0
        with open(host_lines_file, 'r', encoding='utf-8') as f:
            total_hosts = sum(1 for line in f if line.strip())

        self.logger.info(f"新主机: {new_host_count} 个, 旧主机: {old_host_count} 个, 合并后待测试: {total_hosts} 个")

        # 并发获取频道列表，收集成功的主机行
        success_hosts = []
        with ThreadPoolExecutor(max_workers=self.config['max_concurrent_total']) as executor:
            futures = {}
            with open(host_lines_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    future = executor.submit(self.fetch_channels, line, source_type)
                    futures[future] = line

            for future in as_completed(futures):
                line = futures[future]
                try:
                    result = future.result()
                    if result:
                        success_hosts.append(result)
                except Exception as e:
                    self.logger.error(f"处理主机 {line.split('|')[0]} 时出错: {e}")

        succ = len(success_hosts)
        if succ > 0:
            # 生成最终的主机列表（注释头 + 成功主机行）
            with open(old_host_file, 'w', encoding='utf-8') as out:
                # 从 merged_host_file 复制注释头
                with open(merged_host_file, 'r', encoding='utf-8') as mf:
                    for line in mf:
                        if line.startswith('#'):
                            out.write(line)
                        else:
                            break
                # 写入成功主机行
                for line in success_hosts:
                    out.write(line + "\n")
            self.logger.info(f"成功主机已更新：{old_host_file} ({succ} 个)")
        else:
            self.logger.warning("没有成功获取到任何频道，原主机列表保持不变")
            # 如果没有成功主机，恢复旧列表（如果有备份）
            if os.path.exists(backup_file):
                shutil.copy2(backup_file, old_host_file)

        # 更新统计
        self.total_success += succ
        self.total_failed += (total_hosts - succ)

        # 清理临时文件
        for f in [new_host_file, merged_host_file, host_lines_file, success_hosts_tmp, backup_file]:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except:
                pass



    def generate_report(self, status="COMPLETED"):
        """生成 JSON 统计报告"""
        end_time = time.time()
        duration = int(end_time - self.start_time)

        # 统计各类型主机数量
        def count_hosts(file):
            if not os.path.exists(file):
                return 0
            with open(file, 'r', encoding='utf-8') as f:
                return sum(1 for line in f if line.strip() and not line.startswith('#'))

        count_multicast = count_hosts("multicastHost.txt")
        count_hotel = count_hosts("hotelHost.txt")
        count_mqlive = count_hosts("mqliveHost.txt")

        # 统计 host.list 测试地址数量
        def count_test_urls(dir_path):
            hostlist = os.path.join(dir_path, "host.list")
            if not os.path.exists(hostlist):
                return 0
            with open(hostlist, 'r', encoding='utf-8') as f:
                return sum(1 for line in f if line.strip() and ',' in line)

        test_multicast = count_test_urls(self.config['dir_multicast'])
        test_hotel = count_test_urls(self.config['dir_hotel'])
        test_mqlive = count_test_urls(self.config['dir_mqlive'])
        total_test = test_multicast + test_hotel + test_mqlive

        total_hosts = count_multicast + count_hotel + count_mqlive
        total_tasks = self.total_success + self.total_failed
        success_rate = (self.total_success / total_tasks * 100) if total_tasks > 0 else 0

        report = {
            "status": status,
            "version": "3.0.2-py",
            "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
            "duration_seconds": duration,
            "site": self.active_site,
            "statistics": {
                "total_hosts_found": total_hosts,
                "multicast_hosts": count_multicast,
                "hotel_hosts": count_hotel,
                "mqlive_hosts": count_mqlive,
                "total_test_urls": total_test,
                "tasks_succeeded": self.total_success,
                "tasks_failed": self.total_failed,
                "tasks_skipped": self.total_skipped,
                "success_rate_percent": round(success_rate, 2)
            },
            "files": {
                "multicast_list": os.path.join(self.config['dir_multicast'], "host.list"),
                "hotel_list": os.path.join(self.config['dir_hotel'], "host.list"),
                "mqlive_list": os.path.join(self.config['dir_mqlive'], "host.list")
            }
        }

        with open(self.config['stats_json'], 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        self.logger.info(f"JSON 统计报告已生成: {self.config['stats_json']}")

        # 终端输出美观的统计信息
        print("\n" + "="*60)
        print("             📺 IPTV 频道列表获取统计报告")
        print("="*60)
        print(f"日期：       {report['timestamp']}")
        print(f"主站：       {self.active_site}")
        print(f"认证参数：   tk={self.tk[:10]}... (已隐藏部分)")
        print("-"*60)
        print(f"主机统计 (已过滤失效):")
        print(f"  - 组播源：  {count_multicast} 个")
        print(f"  - 酒店源：  {count_hotel} 个")
        print(f"  - 秒播源：  {count_mqlive} 个")
        print("-"*60)
        print(f"结果统计:")
        print(f"  - 测试地址总数：{total_test} 个")
        print(f"    * 组播目录：{test_multicast}")
        print(f"    * 酒店目录：{test_hotel}")
        print(f"    * 秒播目录：{test_mqlive}")
        print(f"  - 成功任务数：   {self.total_success}")
        print(f"  - 失败任务数：   {self.total_failed}")
        print(f"  - 跳过任务数：   {self.total_skipped}")
        print(f"  - 耗时：{duration} 秒")
        print("="*60)
        print(f"详细日志请查看：{self.config['log_file']}")
        print(f"测试地址列表：   {report['files']['multicast_list']}, {report['files']['hotel_list']}, {report['files']['mqlive_list']}")
        print(f"JSON 报告：      {self.config['stats_json']}")
        print("="*60)


# =============================================================================
# 主函数
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="高级 IPTV 频道列表获取脚本")
    parser.add_argument('indices', nargs='?', default='',
                        help='省份序号选择器，如 "1,2,3" "1-5" "all" 或留空（默认精选）')
    parser.add_argument('--province', '-p', action='append', help='指定省份名称（如 北京），可多次使用')
    parser.add_argument('--operator', '-o', choices=['联通', '电信', '移动'], action='append',
                        help='指定运营商，可多次使用')
    parser.add_argument('--type', '-t', choices=['multicast', 'hotel', 'mqlive'], action='append',
                        help='指定要处理的源类型，可多次使用，不指定则处理所有')
    parser.add_argument('--debug', action='store_true', help='启用调试模式')
    parser.add_argument('--config', default='config.json', help='配置文件路径')
    args = parser.parse_args()

    # 加载配置
    config = Config(args.config)

    # 设置日志
    log_file = config['log_file'] if not args.debug else None  # 调试时也可写入文件，但为简化，调试模式仍写入
    logger = setup_logger(debug_mode=args.debug, log_file=config['log_file'])

    # 创建 fetcher 实例
    fetcher = IPTVFetcher(config, debug=args.debug)

    # 认证
    if not fetcher.authenticate():
        sys.exit(1)

    # 构建查询参数列表
    queries = []

    # 如果通过 --province 和 --operator 指定
    if args.province:
        ops = args.operator if args.operator else ['联通', '电信', '移动']
        for prov in args.province:
            for op in ops:
                queries.append(f"{prov}{op}")

    # 如果通过序号选择器指定
    elif args.indices:
        indices = parse_indices(args.indices, len(PROVINCES))
        for idx in indices:
            name, pinyin = PROVINCES[idx]
            for op in OPERATORS:
                queries.append(f"{name}{op}")

    # 默认精选查询
    else:
        queries = ["北京"]  # 可配置为从 config 读取

    # 确定要处理的源类型
    types_to_process = args.type if args.type else ['multicast', 'hotel', 'mqlive']

    # 执行查询
    for query in queries:
        for stype in types_to_process:
            fetcher.process_source_type(stype, query)

    # 生成报告
    fetcher.generate_report()

if __name__ == "__main__":
    main()
