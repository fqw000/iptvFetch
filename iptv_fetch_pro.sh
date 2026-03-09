#!/usr/bin/env bash
#===============================================================================
# Script Name: iptv_fetch_pro.sh
# Description: 高级 IPTV 频道列表获取脚本 (支持组播、酒店、秒播源) - 优化版
# Version:     2.3.0 (测试地址分散到各类型目录)
# Author:      Qwen (Refactored by AI)
# Date:        2026-03-07
# License:     MIT
#===============================================================================

#-------------------------------------------------------------------------------
# 配置区域 (Configuration)
#-------------------------------------------------------------------------------
readonly SCRIPT_VERSION="2.3.0"
readonly SCRIPT_NAME="$(basename "$0")"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 站点配置
readonly MAIN_SITE="https://tonkiang.us"
readonly BACKUP_SITE="http://foodieguide.com/iptvsearch"

# 目录配置
readonly DIR_RESPONSE="response_files"
readonly DIR_MULTICAST="multicastList"
readonly DIR_HOTEL="hotelList"
readonly DIR_MQLIVE="mqliveList"

# 文件配置
readonly FILE_LOG="iptv_fetch.log"
# 不再需要全局测试主机文件
# readonly FILE_TEST_HOST="test_host.list"

# 网络配置
readonly REQ_TIMEOUT=15
readonly REQ_RETRY_COUNT=3
readonly REQ_SLEEP_INTERVAL=1

# 用户代理
readonly USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"

# 全局变量
declare -g TK=""
declare -g CODE=""
declare -g ACTIVE_SITE=""
declare -g CURRENT_DATE=""
declare -i TOTAL_SUCCESS=0
declare -i TOTAL_FAILED=0

# 调试模式 (设置环境变量 DEBUG_MODE=true 开启详细日志)
: "${DEBUG_MODE:=false}"

#-------------------------------------------------------------------------------
# 日志系统 (Logging System)
#-------------------------------------------------------------------------------
log_level() {
    local level="$1"
    shift
    local message="$*"
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    
    local formatted_msg="[${timestamp}] [${level}] [${SCRIPT_NAME}] ${message}"
    
    # 控制台输出 (带颜色)
    case "$level" in
        ERROR) echo -e "\033[31m${formatted_msg}\033[0m" >&2 ;;
        WARN)  echo -e "\033[33m${formatted_msg}\033[0m" ;;
        INFO)  echo -e "\033[32m${formatted_msg}\033[0m" ;;
        DEBUG) 
            if [[ "$DEBUG_MODE" == "true" ]]; then
                echo -e "\033[36m${formatted_msg}\033[0m"
            fi
            ;;

        REQUEST|RESPONSE)
            if [[ "$DEBUG_MODE" == "true" ]]; then
                echo -e "\033[35m${formatted_msg}\033[0m"
            else
                echo -e "\033[32m${formatted_msg}\033[0m"
            fi
            ;;
        # REQUEST) 
        #     if [[ "$DEBUG_MODE" == "true" ]]; then
        #         echo -e "\033[35m${formatted_msg}\033[0m"
        #     else
        #         echo -e "\033[32m[${timestamp}] [REQ] ${message}\033[0m"
        #     fi
        #     ;;
        # RESPONSE)
        #     if [[ "$DEBUG_MODE" == "true" ]]; then
        #         echo -e "\033[35m${formatted_msg}\033[0m"
        #     else
        #         echo -e "\033[32m[${timestamp}] [RSP] ${message}\033[0m"
        #     fi
        #     ;;
        *)     echo "${formatted_msg}" ;;
    esac

    # 文件输出 (纯文本)
    echo "${formatted_msg}" >> "$FILE_LOG"
}

log_info()     { log_level "INFO" "$@"; }
log_warn()     { log_level "WARN" "$@"; }
log_error()    { log_level "ERROR" "$@"; }
log_debug()    { log_level "DEBUG" "$@"; }
log_request()  { log_level "REQUEST" "$@"; }
log_response() { log_level "RESPONSE" "$@"; }

#-------------------------------------------------------------------------------
# 工具函数 (Utilities)
#-------------------------------------------------------------------------------
cleanup() {
    local exit_code=$?
    if [[ $exit_code -ne 0 ]]; then
        log_warn "脚本非正常退出，退出码: $exit_code"
    fi
    # 清理可能遗留的通用锁文件（如 success/fail 计数锁）
    rm -f /tmp/success_*.lock /tmp/fail_*.lock 2>/dev/null

    # 清理各类型目录下的 host.list.lock 文件
    # 使用 find 命令查找并删除，兼容性版本（支持非 GNU 系统）
    find "$DIR_MULTICAST" "$DIR_HOTEL" "$DIR_MQLIVE" -name "host.list.lock" -type f -exec rm -f {} \; 2>/dev/null

    # 若调试模式未开启，删除临时响应文件目录
    if [[ "$DEBUG_MODE" != "true" ]]; then
        log_debug "清理临时响应文件目录: $DIR_RESPONSE"
        rm -rf "$DIR_RESPONSE" 2>/dev/null
    fi

    exit $exit_code
}


init_environment() {
    CURRENT_DATE=$(date +"%Y%m%d_%H%M%S")
    
    mkdir -p "$DIR_RESPONSE" "$DIR_MULTICAST" "$DIR_HOTEL" "$DIR_MQLIVE"
    
    # 初始化日志
    echo "" > "$FILE_LOG"
    log_info "=========================================="
    log_info "IPTV 频道列表获取脚本 v${SCRIPT_VERSION} 启动"
    log_info "执行时间: ${CURRENT_DATE}"
    log_info "工作目录: ${SCRIPT_DIR}"
    log_info "调试模式: ${DEBUG_MODE}"
    log_info "=========================================="
    
    # 不再创建全局测试主机文件
}

# 带重试机制的 Curl 请求 (增强日志)
fetch_url() {
    local url="$1"
    local output_file="$2"
    local retry=0
    local http_code
    
    log_request "准备请求: ${url}"
    log_debug "目标文件: ${output_file}"

    while [[ $retry -lt $REQ_RETRY_COUNT ]]; do
        log_debug "尝试请求 ($((retry + 1))/${REQ_RETRY_COUNT}): ${url}"
        
        http_code=$(curl -s -o "$output_file" -w "%{http_code}" \
            --max-time "$REQ_TIMEOUT" \
            --connect-timeout "$REQ_TIMEOUT" \
            -H "Accept-Language: zh-CN,zh;q=0.9" \
            -A "$USER_AGENT" \
            "$url" 2>/dev/null) || http_code="000"
        
        if [[ "$http_code" == "200" ]] && [[ -s "$output_file" ]]; then
            log_response "成功 | URL: ${url} | 文件: ${output_file} | 大小: $(wc -c < "$output_file") bytes"
            return 0
        fi
        
        log_warn "请求失败 (HTTP: ${http_code}, 文件大小: $(stat -c%s "$output_file" 2>/dev/null || echo 0)) - 重试中..."
        log_debug "失败详情: URL=${url}, Output=${output_file}"
        
        sleep "$REQ_SLEEP_INTERVAL"
        ((retry++))
    done
    
    log_response "失败 | URL: ${url} | 文件: ${output_file} | 最终状态: HTTP ${http_code}"
    log_error "请求最终失败: ${url} (HTTP: ${http_code})"
    return 1
}

#-------------------------------------------------------------------------------
# 核心业务逻辑 (Core Logic)
#-------------------------------------------------------------------------------

# 获取 Tk 和 Code (仅校验 TK)
authenticate() {
    local site="$1"
    local site_name="$2"
    local target_url="${site}/iptvmulticast.php"
    local response_file="${DIR_RESPONSE}/auth_${site_name}_${CURRENT_DATE}.html"
    
    log_info "正在从 [${site_name}] 获取认证参数..."
    
    if ! fetch_url "$target_url" "$response_file"; then
        return 1
    fi
    
    local tk_line
    tk_line=$(grep -o 'channellist.html?ip=[^&]*&tk=[^"&]*' "$response_file" | tail -n 1)
    TK=$(echo "$tk_line" | grep -o 'tk=[^&]*' | cut -d'=' -f2)
    
    CODE=$(grep -o 'code=[^"'\''[:space:]]*' "$response_file" | head -n 1 | cut -d'=' -f2)
    
    if [[ -z "$TK" ]]; then
        log_error "从 ${site_name} 提取 tk 失败 (tk='${TK}')"
        return 1
    fi
    
    ACTIVE_SITE="$site"
    log_info "认证成功: tk=${TK}, code=${CODE:-<空>}，当前站点: ${ACTIVE_SITE}"
    return 0
}

# 获取主机列表 (过滤失效主机)
fetch_hosts() {
    local source_type="$1"
    local url_base
    local p_value
    local output_file="${source_type}Host.txt"
    local temp_file="${output_file}.tmp"
    
    case "$source_type" in
        multicast) url_base="${ACTIVE_SITE}/iptvmulticast.php"; p_value="2" ;;
        hotel)     url_base="${ACTIVE_SITE}/iptvhotelx.php"; p_value="3" ;;
        mqlive)    url_base="${ACTIVE_SITE}/mqlive.php";     p_value="1" ;;
        *)         log_error "无效的来源类型: $source_type"; return 1 ;;
    esac
    
    log_info "开始获取 [${source_type}] 主机列表 (页码 1-5)..."
    > "$temp_file"
    
    for page in {1..5}; do
        local req_url="${url_base}?page=${page}&iphone16=&code=${CODE}"
        local resp_file="${DIR_RESPONSE}/hosts_${source_type}_p${page}_${CURRENT_DATE}.html"
        
        if fetch_url "$req_url" "$resp_file"; then
            log_debug "正在解析第 ${page} 页主机信息..."
            
            perl -0777 -ne '
                while(/<div class="result">(.*?)<\/div>\s*(?=<div class="result"|\z)/gs){
                    $b=$1;

                    if($b =~ /暂时失效/){
                        next;
                    }

                    ($ip)=$b=~/ip=([^&]+)/;
                    
                    next unless defined $ip;

                    ($c)=$b=~/>(\d+)</;
                    
                    if($b=~/新上线/){$s="新上线"}
                    elsif($b=~/存活.*?(\d+).*?天/){$s="存活$1天"}
                    elsif($b=~/存活/){$s="存活"}
                    else{$s="未知"}
                    
                    if($b=~/<i>(.*?)<\/i>/s){
                        $i=$1; $i=~s/^\s+|\s+$//g; $i=~s/\s+/ /g;
                        if($i=~/(\d{4}-\d{2}-\d{2} \d{2}:\d{2})上线\s*(.+)/){
                            $t=$1; $r=$2;
                            if($r=~/(.+?组播)\s+(.+)/){$ty=$1;$is=$2}else{($ty,$is)=split(/\s+/,$r)}
                        }
                    }
                    print "$ip|$c|$t|$s|$ty|$is\n";
                }
            ' "$resp_file" >> "$temp_file"
        else
            log_warn "获取 ${source_type} 第 ${page} 页失败，跳过。"
        fi
        sleep 0.5
    done
    
    if [[ -s "$temp_file" ]]; then
        {
            echo "# ${source_type} 主机信息 (生成时间: ${CURRENT_DATE})"
            echo "# 格式: IP|频道数量|上线时间|状态|类型|运营商"
            echo "# 注意：已自动过滤 '暂时失效' 的主机"
            sort -u "$temp_file"
        } > "$output_file"
        
        local count
        count=$(wc -l < "$output_file")
        local valid_count=$((count - 3))
        log_info "[${source_type}] 成功提取 ${valid_count} 个有效主机 (已过滤失效节点)"
    else
        log_warn "[${source_type}] 未提取到任何有效主机信息"
        echo "# ${source_type} 主机信息 (生成时间: ${CURRENT_DATE}) - 无数据" > "$output_file"
    fi
    
    rm -f "$temp_file"
}

# 获取频道列表 (已修改：根据活跃站点选择API路径，测试地址写入各类型目录下的 host.list)
fetch_channels() {
    local host="$1"
    local source_type="$2"
    local p_value
    
    case "$source_type" in
        multicast) p_value="2" ;;
        hotel)     p_value="1" ;;
        mqlive)    p_value="3" ;;
        *)         return 1 ;;
    esac

    # 根据活跃站点选择正确的 API 路径
    local api_path
    if [[ "$ACTIVE_SITE" == "$MAIN_SITE" ]]; then
        api_path="/getall26.php"
    elif [[ "$ACTIVE_SITE" == "$BACKUP_SITE" ]]; then
        api_path="/getall.php"
    else
        api_path="/getall26.php" # 默认
    fi

    local req_url="${ACTIVE_SITE}${api_path}?ip=${host}&c=&tk=${TK}&p=${p_value}"
    local safe_host="${host//\//_}"
    local resp_file="${DIR_RESPONSE}/channels_${source_type}_${safe_host}_${CURRENT_DATE}.html"
    
    if ! fetch_url "$req_url" "$resp_file"; then
        return 1
    fi
    
    if ! grep -q '<div class="channel"' "$resp_file"; then
        log_debug "主机 ${host} 返回内容无效，跳过。"
        return 1
    fi
    
    if grep -q "暂时失效" "$resp_file"; then
        log_debug "主机 ${host} 状态为暂时失效，跳过。"
        return 1
    fi

    local operator
    operator=$(grep -o '来自<b>[^<]*</b>' "$resp_file" | sed 's/来自<b>\([^<]*\)<\/b>/\1/' | head -1)
    [[ -z "$operator" ]] && operator="Unknown"
    
    local channel_count
    channel_count=$(grep -o '共有<b>[0-9]*</b>' "$resp_file" | sed 's/共有<b>\([0-9]*\)<\/b>/\1/' | head -1)
    [[ -z "$channel_count" ]] && channel_count="0"
    
    local out_dir
    case "$source_type" in
        multicast) out_dir="$DIR_MULTICAST" ;;
        hotel)     out_dir="$DIR_HOTEL" ;;
        mqlive)    out_dir="$DIR_MQLIVE" ;;
    esac
    
    local safe_operator
    safe_operator=$(echo "$operator" | tr -d '/\:*?"<>|')
    local filename="${safe_operator}_${source_type}_${safe_host}_${channel_count}.txt"
    local final_path="${out_dir}/${filename}"
    
    {
        echo "# ${source_type} 频道信息 | 主机: ${host} | 运营商: ${operator}"
        echo "# 频道数: ${channel_count} | 生成时间: ${CURRENT_DATE}"
        echo "# ----------------------------------------"
        
        local names_file urls_file
        names_file=$(mktemp)
        urls_file=$(mktemp)
        
        sed -n 's/.*<div class="tip"[^>]*>\([^<]*\)<\/div>.*/\1/p' "$resp_file" | awk '{print $1}' > "$names_file"
        sed -n 's/.*\(http[^<]*\)<\/td>.*/\1/p' "$resp_file" > "$urls_file"
        
        paste -d ',' "$names_file" "$urls_file"
        
        rm -f "$names_file" "$urls_file"
    } > "$final_path"
    
    # 将第一个有效的频道 URL 追加到对应类型的 host.list 中
    local first_channel
    first_channel=$(grep -v '^#' "$final_path" | head -n 1 )
    # first_first_url=$(grep -v '^#' "$final_path" | head -n 1 | cut -d',' -f2)
    if [[ -n "$first_channel" ]]; then
        local test_host_file="${out_dir}/host.list"
        local lock_file="${test_host_file}.lock"
        # 使用 flock 保证多进程安全追加
        (
            flock -x 200
            echo "$first_channel" >> "$test_host_file"
        ) 200>"$lock_file"
    fi
    
    log_debug "成功保存频道列表: ${final_path} (${channel_count} 个频道)"
    return 0
}

# 并发包装函数：负责调用 fetch_channels 并原子化记录成功/失败
fetch_channels_wrapper() {
    local host_line="$1"
    local source_type="$2"
    local success_file="$3"
    local host
    host=$(echo "$host_line" | cut -d'|' -f1)
    
    log_debug "开始处理主机: ${host} (${source_type})"
    
    if fetch_channels "$host" "$source_type"; then
        # 原子追加成功主机行到临时文件
        (
            flock -x 200
            echo "$host_line" >> "$success_file"
        ) 200>"${success_file}.lock"
    fi
}



# 处理指定来源类型（酒店/秒播/组播）- 并发版本
#-------------------------------------------------------------------------------
# 处理指定来源类型（酒店/秒播/组播）- 优化版（支持合并去重与失效清理）
#-------------------------------------------------------------------------------
process_source_type() {
    local source_type="$1"
    
    log_info "=========================================="
    log_info "开始处理来源类型: [${source_type^^}]"
    log_info "=========================================="
    
    # 定义相关文件路径
    local old_host_file="${source_type}Host.txt"
    local old_host_backup="${source_type}Host_old.tmp"
    local new_host_file="${source_type}Host_new.tmp"
    local merged_host_file="${source_type}Host_merged.tmp"
    local host_lines_file="${source_type}Host_lines.tmp"
    local success_hosts_tmp="${source_type}Host_success.tmp"
    
    # 备份当前有效主机列表
    if [[ -f "$old_host_file" ]]; then
        cp "$old_host_file" "$old_host_backup"
        log_debug "已备份旧主机列表: $old_host_backup"
    fi
    
    # 获取最新主机列表（生成 new_host_file）
    # 注意：fetch_hosts 原本输出到 ${source_type}Host.txt，我们临时重定向
    fetch_hosts "$source_type"  # 该函数会生成 ${source_type}Host.txt
    mv "${source_type}Host.txt" "$new_host_file" 2>/dev/null || true
    
    # 合并新旧主机列表（新文件优先，IP去重）
    {
        echo "# ${source_type} 主机信息 (生成时间: ${CURRENT_DATE})"
        echo "# 格式: IP|端口/数量|上线时间|状态|类型|运营商"
        echo "# 注意：已自动过滤 '暂时失效' 的主机"
    } > "$merged_host_file"
    
    {
        if [[ -s "$new_host_file" ]]; then
            grep -v '^#' "$new_host_file"
        fi
        if [[ -s "$old_host_backup" ]]; then
            grep -v '^#' "$old_host_backup"
        fi
    } | awk -F'|' '!seen[$1]++' >> "$merged_host_file"
    
    # 提取待测试主机列表（仅IP，用于后续并发）
    grep -v '^#' "$merged_host_file" > "$host_lines_file"
    
    local total_hosts
    total_hosts=$(wc -l < "$host_lines_file")
    log_info "包含就主机，共发现 ${total_hosts} 个待测试主机，开始并发获取频道列表（并发数: 5）..."
    
    # 初始化成功主机临时文件
    > "$success_hosts_tmp"

    
    # 导出必要的函数和变量供子进程使用
    export -f fetch_channels fetch_channels_wrapper log_level log_info log_warn log_error log_debug log_request log_response fetch_url
    export MAIN_SITE BACKUP_SITE TK CODE ACTIVE_SITE DIR_RESPONSE DIR_MULTICAST DIR_HOTEL DIR_MQLIVE \
           REQ_SLEEP_INTERVAL REQ_TIMEOUT REQ_RETRY_COUNT USER_AGENT \
           DEBUG_MODE FILE_LOG SCRIPT_NAME CURRENT_DATE PATH
    
    # 使用 xargs 并发执行，每行作为一个参数传递
    if [[ -s "$host_lines_file" ]]; then
        xargs -a "$host_lines_file" -I {} -P 5 bash -c '
            fetch_channels_wrapper "$1" "$2" "$3"
        ' _ {} "$source_type" "$success_hosts_tmp"
    fi
    
    # 读取成功主机数量
    local succ=0
    if [[ -s "$success_hosts_tmp" ]]; then
        succ=$(wc -l < "$success_hosts_tmp")
        # 用成功主机列表覆盖原主机文件
        # 从合并文件中提取注释头（前3行），加上成功主机行，重新生成原主机文件
        {
            head -n 3 "$merged_host_file"   # 提取注释头
            cat "$success_hosts_tmp"        # 成功主机行（已去重）
        } > "$old_host_file"
        
        log_info "成功主机已更新: $old_host_file (${succ} 个)"
    else
        log_warn "没有成功获取到任何频道，原主机列表保持不变: $old_host_file"
        # 清理临时文件，但保留原文件
    fi
    
    # 累加到全局统计
    TOTAL_SUCCESS=$((TOTAL_SUCCESS + succ))
    TOTAL_FAILED=$((TOTAL_FAILED + (total_hosts - succ)))
    
    # 清理临时文件
    rm -f "$new_host_file" "$old_host_backup" "$merged_host_file" "$host_lines_file" "$success_hosts_tmp" "${success_hosts_tmp}.lock"
    
    log_info "[${source_type^^}] 处理完成. 成功: ${succ}, 失败: $((total_hosts - succ))"
}


#-------------------------------------------------------------------------------
# 主程序入口 (Main Entry)
#-------------------------------------------------------------------------------
main() {
    trap cleanup EXIT INT TERM
    
    init_environment
    
    if authenticate "$MAIN_SITE" "tonkiang.us"; then
        :  # 主站成功，ACTIVE_SITE 已设置
    else
        log_warn "主站认证失败，尝试备用站..."
        if authenticate "$BACKUP_SITE" "foodguide"; then
            log_info "备用站认证成功，后续请求将使用: ${ACTIVE_SITE}"
        else
            log_error "所有站点认证失败，脚本终止。"
            exit 1
        fi
    fi
    
    process_source_type "hotel"
    process_source_type "mqlive"
    process_source_type "multicast"
    
    log_info "=========================================="
    log_info "生成最终统计报告"
    log_info "=========================================="
    
    local count_multicast count_hotel count_mqlive
    count_multicast=$(grep -cv '^#' multicastHost.txt 2>/dev/null || echo 0)
    count_hotel=$(grep -cv '^#' hotelHost.txt 2>/dev/null || echo 0)
    count_mqlive=$(grep -cv '^#' mqliveHost.txt 2>/dev/null || echo 0)
    
    local total_files
    total_files=$(find "$DIR_MULTICAST" "$DIR_HOTEL" "$DIR_MQLIVE" -name "*.txt" ! -name "host.list" 2>/dev/null | wc -l)
    
    # 统计各类型目录下的 host.list 测试地址数量
    local test_count_multicast test_count_hotel test_count_mqlive total_test_count
    test_count_multicast=$(grep -c '^http' "${DIR_MULTICAST}/host.list" 2>/dev/null || echo 0)
    test_count_hotel=$(grep -c '^http' "${DIR_HOTEL}/host.list" 2>/dev/null || echo 0)
    test_count_mqlive=$(grep -c '^http' "${DIR_MQLIVE}/host.list" 2>/dev/null || echo 0)
    # total_test_count=$((test_count_multicast + test_count_hotel + test_count_mqlive))
    ((total_test_count = test_count_multicast + test_count_hotel + test_count_mqlive))
    
    cat << EOF
============================================================
             IPTV 频道列表获取统计报告
============================================================
日期:       ${CURRENT_DATE}
主站:       ${MAIN_SITE}
认证参数:   tk=${TK:0:10}... (已隐藏部分)
------------------------------------------------------------
主机统计 (已过滤失效):
  - 组播源:  ${count_multicast} 个
  - 酒店源:  ${count_hotel} 个
  - 秒播源:  ${count_mqlive} 个
------------------------------------------------------------
结果统计:
  - 生成文件总数: ${total_files} 个 (不包含 host.list)
  - 测试地址总数: ${total_test_count} 个 (分布在各类型目录的 host.list 中)
    * 组播目录: ${test_count_multicast}
    * 酒店目录: ${test_count_hotel}
    * 秒播目录: ${test_count_mqlive}
  - 成功任务数:   ${TOTAL_SUCCESS}
  - 失败任务数:   ${TOTAL_FAILED}
============================================================
详细日志请查看: ${FILE_LOG}
测试地址列表:   ${DIR_MULTICAST}/host.list, ${DIR_HOTEL}/host.list, ${DIR_MQLIVE}/host.list
============================================================
EOF
    
    log_info "脚本执行完毕。"
}

main "$@"