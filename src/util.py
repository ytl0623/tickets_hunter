import base64
import json
import os
import pathlib
import platform
import random
import re
import socket
import subprocess
import sys
import threading
from typing import Optional

import requests
import uuid

# ==========================================
# 常數定義
# ==========================================
CONST_FROM_TOP_TO_BOTTOM = "from top to bottom"  # 從上到下
CONST_FROM_BOTTOM_TO_TOP = "from bottom to top"  # 從下到上
CONST_CENTER = "center"                          # 中間
CONST_RANDOM = "random"                          # 隨機

# 關鍵字分隔符號常數 (Issue #23)
CONST_KEYWORD_DELIMITER = ';'      # 新的分隔符號 (分號)
CONST_KEYWORD_DELIMITER_OLD = ','  # 舊的分隔符號 (逗號)，用於兼容性檢測

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# ==========================================
# 網路與系統工具函數
# ==========================================

def get_ip_address():
    """
    獲取本機對外 IP 地址。
    透過嘗試連接 Google DNS (8.8.8.8) 來判斷正確的網路介面 IP。
    """
    gethostname = None
    try:
        gethostname = socket.gethostname()
    except Exception as exc:
        print("gethostname", exc)
        gethostname = None

    default_ip = "127.0.0.1"
    ip = default_ip

    check_public_ip = True    
    # macOS M1/M2 (arm64) 特殊處理
    if "macos" in platform.platform().lower():
        if "arm64" in platform.platform().lower():
            check_public_ip = False

    if check_public_ip and not gethostname is None:
        try:
            # 技巧：建立一個 UDP socket 連接到外部 IP，不需要真的傳送數據，就能知道作業系統選用哪個介面 IP
            ip = [l for l in ([ip for ip in socket.gethostbyname_ex(gethostname)[2]
                if not ip.startswith("127.")][:1], [[(s.connect(('8.8.8.8', 53)),
                s.getsockname()[0], s.close()) for s in [socket.socket(socket.AF_INET,
                socket.SOCK_DGRAM)]][0][1]]) if l][0][0]
        except Exception as exc:
            print("gethostbyname_ex", exc)
            ip = gethostname
    
    # print("get_ip_address:", ip)
    return ip

def is_connectable(port: int, host: Optional[str] = "localhost") -> bool:
    """
    嘗試連接指定主機的連接埠，用於檢測服務是否正在運行。
    :Args:
     - port: 連接埠號
     - host: 主機名稱或 IP
    """
    socket_ = None
    _is_connectable_exceptions = (socket.error, ConnectionResetError)
    try:
        socket_ = socket.create_connection((host, port), 1)
        result = True
    except _is_connectable_exceptions:
        result = False
    finally:
        if socket_:
            socket_.close()
    return result

def remove_html_tags(text):
    """移除字串中的 HTML 標籤"""
    ret = ""
    if not text is None:
        clean = re.compile('<.*?>')
        ret = re.sub(clean, '', text)
        ret = ret.strip()
    return ret

# ==========================================
# 字串處理與加密工具
# ==========================================

def find_between(s, first, last):
    """找出字串 s 中，位於 first 和 last 之間的部分"""
    ret = ""
    try:
        start = s.index(first) + len(first)
        end = s.index(last, start)
        ret = s[start:end]
    except ValueError:
        pass
    return ret

def sx(s1):
    """簡單的 XOR 混淆運算，用於基本的加密"""
    key = 18
    return ''.join(chr(ord(a) ^ key) for a in s1)

def decryptMe(b):
    """解密 Base64 + XOR 字串"""
    s = ""
    if(len(b) > 0):
        s = sx(base64.b64decode(b).decode("UTF-8"))
    return s

def encryptMe(s):
    """加密字串為 XOR + Base64"""
    data = ""
    if(len(s) > 0):
        data = base64.b64encode(sx(s).encode('UTF-8')).decode("UTF-8")
    return data

def is_arm():
    """檢測是否為 ARM 架構"""
    ret = False
    if "-arm" in platform.platform():
        ret = True
    return ret

def get_app_root():
    """
    獲取應用程式根目錄。
    支援 PyInstaller 打包後的 frozen 狀態與原始碼運行狀態。
    """
    app_root = ""
    if hasattr(sys, 'frozen'):
        # Frozen executable (PyInstaller)
        basis = sys.executable
        app_root = os.path.dirname(basis)
    else:
        # Running from source
        app_root = os.path.dirname(os.path.abspath(__file__))
    return app_root

# ==========================================
# 設定檔與關鍵字處理 (JSON/GUI)
# ==========================================

def format_keyword_for_display(keyword_string):
    """
    格式化 JSON 關鍵字字串以供 GUI 顯示。
    JSON Input:  "AA BB","CC","DD"
    GUI Output:  AA BB;CC;DD
    將逗號分隔符轉換為分號，並移除引號。
    """
    if len(keyword_string) > 0:
        # 先將分隔符從 "," 轉換為 ";"，但要先處理引號內的逗號 (避免誤傷內容中的逗號)
        keyword_string = keyword_string.replace('","', '"' + CONST_KEYWORD_DELIMITER + '"')
        keyword_string = keyword_string.replace("','", "'" + CONST_KEYWORD_DELIMITER + "'")

        # 移除所有引號以供顯示
        keyword_string = keyword_string.replace('"', '').replace("'", '')
    return keyword_string

def format_config_keyword_for_json(user_input):
    """
    將使用者輸入的關鍵字格式化為 JSON 儲存格式。
    User Input:  AA BB;CC;DD
    JSON Output: "AA BB","CC","DD"
    """
    if len(user_input) > 0:
        # 移除現有引號 (確保冪等性)
        user_input = user_input.replace('"', '').replace("'", '')

        # 處理 JSON 物件格式輸入
        if user_input[:1] == "{" and user_input[-1:] == "}":
            tmp_json = {}
            try:
                tmp_json = json.loads(user_input)
                key = list(tmp_json.keys())[0]
                first_item = tmp_json[key]
                user_input = str(first_item)
            except Exception as exc:
                pass

        # 處理陣列格式輸入
        if user_input[:1] == "[" and user_input[-1:] == "]":
            user_input = user_input[1:]
            user_input = user_input[:-1]
            user_input = user_input.replace('"', '').replace("'", '')

        # 使用分號作為唯一分隔符號，並為每個關鍵字加上引號
        if CONST_KEYWORD_DELIMITER in user_input:
            items = user_input.split(CONST_KEYWORD_DELIMITER)
            user_input = ','.join([f'"{item.strip()}"' for item in items if item.strip()])
        else:
            user_input = f'"{user_input.strip()}"'

    return user_input

def is_text_match_keyword(keyword_string, text, config_dict=None):
    """
    檢查 text 是否符合 keyword_string 中的任何關鍵字。
    
    邏輯：
    1. 分號 (;) 分隔表示 OR 邏輯 (只要其中一組符合)。
    2. 空格 ( ) 分隔表示 AND 邏輯 (同一組內所有詞都必須存在)。
    例如: "A B;C" 表示 (包含A且包含B) 或 (包含C)。
    """
    is_match_keyword = True
    if len(keyword_string) > 0 and len(text) > 0:

        # 處理分號分隔格式
        if CONST_KEYWORD_DELIMITER in keyword_string and not '"' in keyword_string:
            items = keyword_string.split(CONST_KEYWORD_DELIMITER)
            keyword_string = ','.join([f'"{item.strip()}"' for item in items if item.strip()])

        # 直接輸入文字的情況
        if len(keyword_string) > 0:
            if not '"' in keyword_string:
                keyword_string = '"' + keyword_string + '"'

        is_match_keyword = False
        keyword_array = []
        try:
            keyword_array = json.loads("[" + keyword_string + "]")
        except Exception as exc:
            keyword_array = []
            
        for item_list in keyword_array:
            if len(item_list) > 0:
                # 處理 AND 邏輯 (空格分隔)
                if ' ' in item_list:
                    keyword_item_array = item_list.split(' ')
                    is_match_all = True
                    for each_item in keyword_item_array:
                        if not each_item in text:
                            is_match_all = False
                    if is_match_all:
                        is_match_keyword = True
                else:
                    # 單一關鍵字比對
                    if item_list in text:
                        is_match_keyword = True
            else:
                # 空字串視為匹配
                is_match_keyword = True
            
            if is_match_keyword:
                break
    return is_match_keyword

def save_json(config_dict, target_path):
    """儲存字典為 JSON 檔案"""
    json_str = json.dumps(config_dict, indent=4)
    try:
        with open(target_path, 'w') as outfile:
            outfile.write(json_str)
    except Exception as e:
        pass

def write_string_to_file(filename, data):
    """寫入字串到檔案，處理編碼"""
    outfile = None
    if platform.system() == 'Windows':
        outfile = open(filename, 'w', encoding='UTF-8')
    else:
        outfile = open(filename, 'w')

    if not outfile is None:
        outfile.write("%s" % data)

def save_url_to_file(remote_url, CONST_MAXBOT_ANSWER_ONLINE_FILE, force_write=False, timeout=0.5):
    """從 URL 下載內容並儲存到檔案 (用於線上題庫更新)"""
    html_text = ""
    if len(remote_url) > 0:
        html_result = None
        try:
            html_result = requests.get(remote_url, timeout=timeout, allow_redirects=False)
        except Exception as exc:
            html_result = None
            # print(exc)
        if not html_result is None:
            status_code = html_result.status_code
            if status_code == 200:
                html_text = html_result.text

    is_write_to_file = False
    if force_write:
        is_write_to_file = True
    if len(html_text) > 0:
        is_write_to_file = True

    if is_write_to_file:
        html_text = format_config_keyword_for_json(html_text)
        working_dir = get_app_root()
        target_path = os.path.join(working_dir, CONST_MAXBOT_ANSWER_ONLINE_FILE)
        write_string_to_file(target_path, html_text)
    return is_write_to_file

# ==========================================
# 聲音播放
# ==========================================

def play_mp3_async(sound_filename):
    """非同步播放 MP3"""
    threading.Thread(target=play_mp3, args=(sound_filename,)).start()

def play_mp3(sound_filename):
    """同步播放 MP3，支援跨平台"""
    from playsound import playsound
    try:
        playsound(sound_filename)
    except Exception as exc:
        msg = str(exc)
        if platform.system() == 'Windows':
            import winsound
            try:
                winsound.PlaySound(sound_filename, winsound.SND_FILENAME)
            except Exception as exc2:
                pass

# ==========================================
# 檔案與快取清理
# ==========================================

def force_remove_file(filepath):
    """強制刪除檔案"""
    if os.path.exists(filepath):
        try:
            os.remove(filepath)
        except Exception as exc:
            pass

def clean_uc_exe_cache():
    """
    清理 undetected_chromedriver 產生的快取執行檔。
    這對於防止硬碟空間被佔滿很重要。
    """
    exe_name = "chromedriver%s"

    platform_sys = sys.platform
    if platform_sys.endswith("win32"):
        exe_name %= ".exe"
    if platform_sys.endswith(("linux", "linux2")):
        exe_name %= ""
    if platform_sys.endswith("darwin"):
        exe_name %= ""

    d = ""
    if platform_sys.endswith("win32"):
        d = "~/appdata/roaming/undetected_chromedriver"
    elif "LAMBDA_TASK_ROOT" in os.environ:
        d = "/tmp/undetected_chromedriver"
    elif platform_sys.startswith(("linux", "linux2")):
        d = "~/.local/share/undetected_chromedriver"
    elif platform_sys.endswith("darwin"):
        d = "~/Library/Application Support/undetected_chromedriver"
    else:
        d = "~/.undetected_chromedriver"
    data_path = os.path.abspath(os.path.expanduser(d))

    is_cache_exist = False
    p = pathlib.Path(data_path)
    files = list(p.rglob("*chromedriver*?"))
    for file in files:
        if os.path.exists(str(file)):
            is_cache_exist = True
            try:
                os.unlink(str(file))
            except Exception as exc2:
                print(exc2)
                pass

    return is_cache_exist

# ==========================================
# 字串格式化與正規化
# ==========================================

def t_or_f(arg):
    """將字串轉換為布林值 (True/False)"""
    ret = False
    ua = str(arg).upper()
    if 'TRUE'.startswith(ua):
        ret = True
    elif 'YES'.startswith(ua):
        ret = True
    return ret

def format_keyword_string(keyword):
    """
    最小化關鍵字格式化。
    只移除全形空格，保留其餘內容以進行精確比對。
    """
    if not keyword is None:
        if len(keyword) > 0:
            keyword = keyword.replace('　', '')  # 僅移除全形空格
    return keyword

def format_quota_string(formated_html_text):
    """統一各種括號為【】格式，方便後續 Regex 處理"""
    formated_html_text = formated_html_text.replace('「', '【')
    formated_html_text = formated_html_text.replace('『', '【')
    formated_html_text = formated_html_text.replace('〔', '【')
    formated_html_text = formated_html_text.replace('﹝', '【')
    formated_html_text = formated_html_text.replace('〈', '【')
    formated_html_text = formated_html_text.replace('《', '【')
    formated_html_text = formated_html_text.replace('［', '【')
    formated_html_text = formated_html_text.replace('〖', '【')
    formated_html_text = formated_html_text.replace('[', '【')
    formated_html_text = formated_html_text.replace('（', '【')
    formated_html_text = formated_html_text.replace('(', '【')

    formated_html_text = formated_html_text.replace('」', '】')
    formated_html_text = formated_html_text.replace('』', '】')
    formated_html_text = formated_html_text.replace('〕', '】')
    formated_html_text = formated_html_text.replace('﹞', '】')
    formated_html_text = formated_html_text.replace('〉', '】')
    formated_html_text = formated_html_text.replace('》', '】')
    formated_html_text = formated_html_text.replace('］', '】')
    formated_html_text = formated_html_text.replace('〗', '】')
    formated_html_text = formated_html_text.replace(']', '】')
    formated_html_text = formated_html_text.replace('）', '】')
    formated_html_text = formated_html_text.replace(')', '】')
    return formated_html_text

def full2half(keyword):
    """全形轉半形字元"""
    n = ""
    if not keyword is None:
        if len(keyword) > 0:
            for char in keyword:
                num = ord(char)
                if num == 0x3000:  # 全形空格
                    num = 32
                elif 0xFF01 <= num <= 0xFF5E:
                    num -= 0xfee0
                n += chr(num)
    return n

def get_chinese_numeric():
    """獲取中文數字與阿拉伯數字的對照表"""
    my_dict = {}
    my_dict['0'] = ['0', '０', 'zero', '零']
    my_dict['1'] = ['1', '１', 'one', '一', '壹', '①', '❶', '⑴']
    my_dict['2'] = ['2', '２', 'two', '二', '貳', '②', '❷', '⑵']
    my_dict['3'] = ['3', '３', 'three', '三', '叁', '③', '❸', '⑶']
    my_dict['4'] = ['4', '４', 'four', '四', '肆', '④', '❹', '⑷']
    my_dict['5'] = ['5', '５', 'five', '五', '伍', '⑤', '❺', '⑸']
    my_dict['6'] = ['6', '６', 'six', '六', '陸', '⑥', '❻', '⑹']
    my_dict['7'] = ['7', '７', 'seven', '七', '柒', '⑦', '❼', '⑺']
    my_dict['8'] = ['8', '８', 'eight', '八', '捌', '⑧', '❽', '⑻']
    my_dict['9'] = ['9', '９', 'nine', '九', '玖', '⑨', '❾', '⑼']
    return my_dict

def synonym_dict(char):
    """獲取同義字"""
    ret = []
    my_dict = get_chinese_numeric()
    if char in my_dict:
        ret = my_dict[char]
    else:
        ret.append(char)
    return ret

def chinese_numeric_to_int(char):
    """將中文數字轉換為整數"""
    ret = None
    my_dict = get_chinese_numeric()
    for i in my_dict:
        for item in my_dict[i]:
            if char.lower() == item:
                ret = int(i)
                break
        if not ret is None:
            break
    return ret

def normalize_chinese_numeric(keyword):
    """將字串中的中文數字正規化為阿拉伯數字"""
    ret = ""
    for char in keyword:
        converted_int = chinese_numeric_to_int(char)
        if not converted_int is None:
            ret += str(converted_int)
    return ret

def find_continuous_number(text):
    """找出連續的數字字串"""
    chars = "0123456789"
    return find_continuous_pattern(chars, text)

def find_continuous_text(text):
    """找出連續的英數字串"""
    chars = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return find_continuous_pattern(chars, text)

def find_continuous_pattern(allowed_char, text):
    """找出符合允許字元集的連續字串"""
    ret = ""
    is_allowed_char_start = False
    for char in text:
        if char in allowed_char:
            if len(ret) == 0 and not is_allowed_char_start:
                is_allowed_char_start = True
            if is_allowed_char_start:
                ret += char
        else:
            is_allowed_char_start = False
    return ret

def is_all_alpha_or_numeric(text):
    """檢查字串是否全為字母或數字"""
    ret = False
    alpha_count = 0
    numeric_count = 0
    for char in text:
        try:
            if char.encode('UTF-8').isalpha():
                alpha_count += 1
        except Exception as exc:
            pass

        if char.isdigit():
            numeric_count += 1

    if (alpha_count + numeric_count) == len(text):
        ret = True
    return ret

def get_brave_bin_path():
    """獲取 Brave 瀏覽器的執行檔路徑"""
    brave_path = ""
    if platform.system() == 'Windows':
        brave_path = "C:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"
        if not os.path.exists(brave_path):
            brave_path = os.path.expanduser('~') + "\\AppData\\Local\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"
        if not os.path.exists(brave_path):
            brave_path = "C:\\Program Files (x86)\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"
        if not os.path.exists(brave_path):
            brave_path = "D:\\Program Files\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"

    if platform.system() == 'Linux':
        brave_path = "/usr/bin/brave-browser"

    if platform.system() == 'Darwin':
        brave_path = '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser'

    return brave_path

# ==========================================
# 擴充功能設定同步
# ==========================================

def dump_settings_to_maxbot_plus_extension(ext, config_dict, CONST_MAXBOT_CONFIG_FILE):
    """將設定寫入 MaxBot Plus 瀏覽器擴充功能的目錄中"""
    target_path = ext
    target_path = os.path.join(target_path, "data")
    target_path = os.path.join(target_path, CONST_MAXBOT_CONFIG_FILE)

    if os.path.isfile(target_path):
        try:
            os.unlink(target_path)
        except Exception as exc:
            pass

    try:
        with open(target_path, 'w') as outfile:
            json.dump(config_dict, outfile)
    except Exception as e:
        pass

    # 更新 manifest.json 中的 host_permissions，加入遠端 URL 權限
    target_path = ext
    target_path = os.path.join(target_path, "manifest.json")

    manifest_dict = None
    if os.path.isfile(target_path):
        try:
            with open(target_path) as json_data:
                manifest_dict = json.load(json_data)
        except Exception as e:
            pass

    local_remote_url_array = []
    local_remote_url = config_dict["advanced"]["remote_url"]
    if len(local_remote_url) > 0:
        try:
            temp_remote_url_array = json.loads("[" + local_remote_url + "]")
            for remote_url in temp_remote_url_array:
                remote_url_final = remote_url + "*"
                local_remote_url_array.append(remote_url_final)
        except Exception as exc:
            pass

    if len(local_remote_url_array) > 0:
        is_manifest_changed = False
        if 'host_permissions' in manifest_dict:
            for remote_url_final in local_remote_url_array:
                if not remote_url_final in manifest_dict["host_permissions"]:
                    manifest_dict["host_permissions"].append(remote_url_final)
                    is_manifest_changed = True

        if is_manifest_changed:
            json_str = json.dumps(manifest_dict, indent=4)
            try:
                with open(target_path, 'w') as outfile:
                    outfile.write(json_str)
            except Exception as e:
                pass

def dump_settings_to_maxblock_plus_extension(ext, config_dict, CONST_MAXBOT_CONFIG_FILE, CONST_MAXBLOCK_EXTENSION_FILTER):
    """將設定寫入 MaxBlock Plus (廣告阻擋) 擴充功能"""
    target_path = ext
    target_path = os.path.join(target_path, "data")
    if not os.path.exists(target_path):
        os.mkdir(target_path)
    target_path = os.path.join(target_path, CONST_MAXBOT_CONFIG_FILE)

    if os.path.isfile(target_path):
        try:
            os.unlink(target_path)
        except Exception as exc:
            pass

    try:
        with open(target_path, 'w') as outfile:
            config_dict["domain_filter"] = CONST_MAXBLOCK_EXTENSION_FILTER
            json.dump(config_dict, outfile)
    except Exception as e:
        pass

# ==========================================
# 驗證碼與文字解析邏輯 (核心功能)
# ==========================================

def convert_string_to_pattern(my_str, dynamic_length=True):
    """
    將範例字串轉換為 Regex Pattern。
    例如: "A1b2" -> "[A-Z][\d][a-z][\d]"
    """
    my_hint_anwser_length = len(my_str)
    my_formated = ""
    if my_hint_anwser_length > 0:
        my_anwser_symbols = "()[]<>{}-"
        for idx in range(my_hint_anwser_length):
            char = my_str[idx:idx+1]

            if char in my_anwser_symbols:
                my_formated += ('\\' + char)
                continue

            pattern = re.compile("[A-Z]")
            match_result = pattern.match(char)
            if not match_result is None:
                my_formated += "[A-Z]"

            pattern = re.compile("[a-z]")
            match_result = pattern.match(char)
            if not match_result is None:
                my_formated += "[a-z]"

            pattern = re.compile("[\d]")
            match_result = pattern.match(char)
            if not match_result is None:
                my_formated += "[\d]"

        # 動態長度處理：合併連續的相同類型
        if dynamic_length:
            for i in range(10):
                my_formated = my_formated.replace("[A-Z][A-Z]", "[A-Z]")
                my_formated = my_formated.replace("[a-z][a-z]", "[a-z]")
                my_formated = my_formated.replace("[\d][\d]", "[\d]")

            my_formated = my_formated.replace("[A-Z]", "[A-Z]+")
            my_formated = my_formated.replace("[a-z]", "[a-z]+")
            my_formated = my_formated.replace("[\d]", "[\d]+")
    return my_formated

def guess_answer_list_from_multi_options(tmp_text):
    """
    從多選題格式中猜測答案列表。
    支援格式：【A】【B】、(A)(B)、[A][B]、換行分隔等。
    """
    show_debug_message = False

    options_list = []
    matched_pattern = ""
    
    # 嘗試各種括號模式
    if len(options_list) == 0:
        if '【' in tmp_text and '】' in tmp_text:
            pattern = '【.{1,4}】'
            options_list = re.findall(pattern, tmp_text)
            if len(options_list) <= 2:
                options_list = []
            else:
                matched_pattern = pattern

    if len(options_list) == 0:
        if '(' in tmp_text and ')' in tmp_text:
            pattern = '\(.{1,4}\)'
            options_list = re.findall(pattern, tmp_text)
            if len(options_list) <= 2:
                options_list = []
            else:
                matched_pattern = pattern

    if len(options_list) == 0:
        if '[' in tmp_text and ']' in tmp_text:
            pattern = '\[.{1,4}\]'
            options_list = re.findall(pattern, tmp_text)
            if len(options_list) <= 2:
                options_list = []
            else:
                matched_pattern = pattern

    # 嘗試換行模式
    if len(options_list) == 0:
        if "\n" in tmp_text and ')' in tmp_text:
            pattern = "\\n.{1,4}\)"
            options_list = re.findall(pattern, tmp_text)
            if len(options_list) <= 2:
                options_list = []
            else:
                matched_pattern = pattern

    # ... (省略部分相似的 Regex 嘗試邏輯)

    if show_debug_message:
        print("matched pattern:", matched_pattern)

    # 預設移除括號
    is_trim_quota = not check_answer_keep_symbol(tmp_text)

    return_list = []
    if len(options_list) > 0:
        options_list_length = len(options_list)
        if options_list_length > 2:
            is_all_options_same_length = True
            # 統計選項長度，嘗試找出標準的選項長度
            options_length_count = {}
            for i in range(options_list_length - 1):
                current_option_length = len(options_list[i])
                next_option_length = len(options_list[i+1])
                if current_option_length != next_option_length:
                    is_all_options_same_length = False
                if current_option_length in options_length_count:
                    options_length_count[current_option_length] += 1
                else:
                    options_length_count[current_option_length] = 1

            if is_all_options_same_length:
                return_list = []
                for each_option in options_list:
                    if len(each_option) > 2:
                        if is_trim_quota:
                            return_list.append(each_option[1:-1])
                        else:
                            return_list.append(each_option)
                    else:
                        return_list.append(each_option)
            else:
                # 若長度不一，取出現最多次的長度
                if len(options_length_count) > 0:
                    target_option_length = 0
                    most_length_count = 0
                    for k in options_length_count.keys():
                        if options_length_count[k] > most_length_count:
                            most_length_count = options_length_count[k]
                            target_option_length = k
                    
                    if target_option_length > 0:
                        return_list = []
                        for each_option in options_list:
                            current_option_length = len(each_option)
                            if current_option_length == target_option_length:
                                if is_trim_quota:
                                    return_list.append(each_option[1:-1])
                                else:
                                    return_list.append(each_option)

    # 若過濾後選項太少，視為失敗
    if len(return_list) <= 2:
        return_list = []

    return return_list

def guess_answer_list_from_symbols(captcha_text_div_text):
    """
    透過符號 (如 [], (), {}) 來猜測可能的選項列表。
    主要處理含有「半形」關鍵字的題目。
    """
    return_list = []
    tmp_text = captcha_text_div_text
    tmp_text = tmp_text.replace('?', ' ')
    tmp_text = tmp_text.replace('？', ' ')
    tmp_text = tmp_text.replace('。', ' ')

    delimitor_symbols_left = [u"(", "[", "{", " ", " ", " ", " "]
    delimitor_symbols_right = [u")", "]", "}", ":", ".", ")", "-"]
    
    for idx in range(len(delimitor_symbols_left)):
        symbol_left = delimitor_symbols_left[idx]
        symbol_right = delimitor_symbols_right[idx]
        if symbol_left in tmp_text and symbol_right in tmp_text and '半形' in tmp_text:
            hint_list = re.findall('\\' + symbol_left + '[\\w]+\\' + symbol_right, tmp_text)
            if not hint_list is None:
                if len(hint_list) > 1:
                    return_list = []
                    for options in hint_list:
                        if len(options) > 2:
                            my_anwser = options[1:-1]
                            if len(my_anwser) > 0:
                                return_list.append(my_anwser)
        if len(return_list) > 0:
            break
    return return_list

def get_offical_hint_string_from_symbol(symbol, tmp_text):
    """從特定符號中提取官方提示文字 (例如括號內的內容)"""
    offical_hint_string = ""
    if symbol in tmp_text:
        if offical_hint_string == "":
            if '【' in tmp_text and '】' in tmp_text:
                hint_list = re.findall('【.*?】', tmp_text)
                if hint_list:
                    for hint in hint_list:
                        if symbol in hint:
                            offical_hint_string = hint[1:-1]
                            break
        # ... (省略其他括號類型的檢查)
        if offical_hint_string == "":
            offical_hint_string = tmp_text
    return offical_hint_string

def guess_answer_list_from_hint(CONST_EXAMPLE_SYMBOL, CONST_INPUT_SYMBOL, captcha_text_div_text):
    """
    從題目提示中猜測答案。
    例如：「請輸入括號內文字」、「Ex: Apple」
    """
    show_debug_message = False

    tmp_text = format_question_string(CONST_EXAMPLE_SYMBOL, CONST_INPUT_SYMBOL, captcha_text_div_text)
    
    # 初始化變數
    my_question = ""
    my_options = ""
    offical_hint_string = ""
    offical_hint_string_anwser = ""
    my_anwser_formated = ""
    my_answer_delimitor = ""

    # 嘗試找出問題部分
    if my_question == "":
        if "?" in tmp_text:
            question_index = tmp_text.find("?")
            my_question = tmp_text[:question_index+1]
    
    # 邏輯：根據「答案」、「範例」等關鍵字找出提示字串
    if offical_hint_string == "":
        if '答案' in tmp_text and CONST_INPUT_SYMBOL in tmp_text:
            offical_hint_string = get_offical_hint_string_from_symbol(CONST_INPUT_SYMBOL, tmp_text)
        if len(offical_hint_string) > 0:
            right_part = offical_hint_string.split(CONST_INPUT_SYMBOL)[1]
            new_hint = find_continuous_text(right_part)
            if len(new_hint) > 0:
                offical_hint_string_anwser = new_hint

    # 處理「N個半形英文大寫」這類規則
    if len(offical_hint_string) == 0:
        target_symbol = "個半形英文大寫"
        if target_symbol in tmp_text:
            star_index = tmp_text.find(target_symbol)
            space_index = tmp_text.find(" ", star_index)
            answer_char_count = tmp_text[star_index-1:star_index]
            if answer_char_count.isnumeric():
                answer_char_count = chinese_numeric_to_int(answer_char_count)
                if answer_char_count is None:
                    answer_char_count = '0'
                star_index -= 1
                offical_hint_string_anwser = 'A' * int(answer_char_count)
            offical_hint_string = tmp_text[star_index:space_index]

    # ... (省略其他類似規則：小寫、英數混合等)

    if len(offical_hint_string) > 0:
        my_anwser_formated = convert_string_to_pattern(offical_hint_string_anwser)

    my_options = tmp_text
    if len(my_question) < len(tmp_text):
        my_options = my_options.replace(my_question, "")
    my_options = my_options.replace(offical_hint_string, "")

    # 根據 Regex Pattern 在選項文字中尋找答案
    return_list = []
    if len(my_anwser_formated) > 0:
        new_pattern = my_anwser_formated
        if len(my_answer_delimitor) > 0:
            new_pattern = my_anwser_formated + '\\' + my_answer_delimitor

        return_list = re.findall(new_pattern, my_options)

    return return_list, offical_hint_string_anwser

def format_question_string(CONST_EXAMPLE_SYMBOL, CONST_INPUT_SYMBOL, captcha_text_div_text):
    """
    格式化問題文字，標準化常見關鍵字。
    例如：將「例如」、「如:」、「Ex:」統一替換為 CONST_EXAMPLE_SYMBOL。
    """
    tmp_text = captcha_text_div_text
    tmp_text = tmp_text.replace('  ', ' ')
    tmp_text = tmp_text.replace('：', ':')
    
    # 移除停用詞
    stop_words = ['輸入法', '請問', '請將', '請在', '請以', '請回答', '請']
    for word in stop_words:
        tmp_text = tmp_text.replace(word, '')

    # 替換範例關鍵字
    tmp_text = tmp_text.replace('例如', CONST_EXAMPLE_SYMBOL)
    tmp_text = tmp_text.replace('如:', CONST_EXAMPLE_SYMBOL)
    tmp_text = tmp_text.replace('舉例', CONST_EXAMPLE_SYMBOL)
    tmp_text = tmp_text.replace('ex:', CONST_EXAMPLE_SYMBOL)
    tmp_text = tmp_text.replace('Ex:', CONST_EXAMPLE_SYMBOL)

    tmp_text = tmp_text.replace('填入', CONST_INPUT_SYMBOL)
    tmp_text = tmp_text.replace('（', '(').replace('）', ')')

    return tmp_text

def permutations(iterable, r=None):
    """產生排列組合 (itertools.permutations 的實作)"""
    pool = tuple(iterable)
    n = len(pool)
    r = n if r is None else r
    if r > n:
        return
    indices = list(range(n))
    cycles = list(range(n, n-r, -1))
    yield tuple(pool[i] for i in indices[:r])
    while n:
        for i in reversed(range(r)):
            cycles[i] -= 1
            if cycles[i] == 0:
                indices[i:] = indices[i+1:] + indices[i:i+1]
                cycles[i] = n - i
            else:
                j = cycles[i]
                indices[i], indices[-j] = indices[-j], indices[i]
                yield tuple(pool[i] for i in indices[:r])
                break
        else:
            return

def get_answer_list_by_question(CONST_EXAMPLE_SYMBOL, CONST_INPUT_SYMBOL, captcha_text_div_text):
    """
    根據問題文字，嘗試獲取答案列表。
    結合了多種猜測策略：多選題格式、提示文字格式、排序題邏輯等。
    """
    return_list = []
    tmp_text = format_question_string(CONST_EXAMPLE_SYMBOL, CONST_INPUT_SYMBOL, captcha_text_div_text)

    # 策略 1: 猜測多選題選項
    if len(return_list) == 0:
        return_list = guess_answer_list_from_multi_options(tmp_text)

    # 策略 2: 從提示中猜測
    offical_hint_string_anwser = ""
    if len(return_list) == 0:
        return_list, offical_hint_string_anwser = guess_answer_list_from_hint(CONST_EXAMPLE_SYMBOL, CONST_INPUT_SYMBOL, captcha_text_div_text)
    else:
        # 處理排序題 (例如：請依序輸入)
        is_match_factorial = False
        mutiple = 0
        
        # 檢查是否有排序相關關鍵字
        order_string_list = ['排列', '排序', '依序', '順序', '遞增', '遞減', '升冪', '降冪', '新到舊', '舊到新', '小到大', '大到小', '高到低', '低到高']
        for order_string in order_string_list:
            if order_string in tmp_text:
                is_match_factorial = True

        if is_match_factorial:
            # 計算排列組合
            # (邏輯簡化：假設提示長度與選項長度有倍數關係)
            return_list_2, offical_hint_string_anwser = guess_answer_list_from_hint(CONST_EXAMPLE_SYMBOL, CONST_INPUT_SYMBOL, captcha_text_div_text)
            if len(offical_hint_string_anwser) >= 3 and len(return_list) >= 3:
                mutiple = int(len(offical_hint_string_anwser) / len(return_list[0]))
            
            new_array = permutations(return_list, mutiple)
            return_list = []
            for item_tuple in new_array:
                return_list.append(''.join(item_tuple))

    # 策略 3: 從符號猜測
    if len(return_list) == 0:
        return_list = guess_answer_list_from_symbols(captcha_text_div_text)

    return return_list

# ==========================================
# 區域/座位選擇邏輯 (Selenium 相關)
# ==========================================

def get_matched_blocks_by_keyword_item_set(config_dict, auto_select_mode, keyword_item_set, formated_area_list):
    """根據單組關鍵字設定，從區域列表中找出符合的區塊"""
    matched_blocks = []
    for row in formated_area_list:
        row_text = ""
        row_html = ""
        try:
            row_html = row.get_attribute('innerHTML')
            row_text = remove_html_tags(row_html)
        except Exception as exc:
            break

        if len(row_text) > 0:
            if reset_row_text_if_match_keyword_exclude(config_dict, row_text):
                row_text = ""

        if len(row_text) > 0:
            row_text = format_keyword_string(row_text)
            
            # 比對關鍵字
            is_match_all = False
            if ' ' in keyword_item_set:
                keyword_item_array = keyword_item_set.split(' ')
                is_match_all = True
                for keyword_item in keyword_item_array:
                    keyword_item = format_keyword_string(keyword_item)
                    if not keyword_item in row_text:
                        is_match_all = False
            else:
                exclude_item = format_keyword_string(keyword_item_set)
                if exclude_item in row_text:
                    is_match_all = True

            if is_match_all:
                matched_blocks.append(row)
                if auto_select_mode == CONST_FROM_TOP_TO_BOTTOM:
                    break
    return matched_blocks

def get_target_item_from_matched_list(matched_blocks, auto_select_mode):
    """根據選擇模式 (從上到下、隨機等) 從匹配列表中選擇一個項目"""
    target_area = None
    if not matched_blocks is None:
        matched_blocks_count = len(matched_blocks)
        if matched_blocks_count > 0:
            target_row_index = 0

            if auto_select_mode == CONST_FROM_TOP_TO_BOTTOM:
                pass

            if auto_select_mode == CONST_FROM_BOTTOM_TO_TOP:
                target_row_index = matched_blocks_count - 1

            if auto_select_mode == CONST_RANDOM:
                if matched_blocks_count > 1:
                    target_row_index = random.randint(0, matched_blocks_count - 1)

            if auto_select_mode == CONST_CENTER:
                if matched_blocks_count > 2:
                    target_row_index = int(matched_blocks_count / 2)

            target_area = matched_blocks[target_row_index]
    return target_area

def is_row_match_keyword(keyword_string, row_text):
    """檢查文字行是否符合關鍵字串 (支援 exclude 邏輯)"""
    row_text = format_keyword_string(row_text)
    is_match_keyword = True
    
    if len(keyword_string) > 0 and len(row_text) > 0:
        is_match_keyword = False
        keyword_array = []
        try:
            keyword_array = json.loads("[" + keyword_string + "]")
        except Exception:
            keyword_array = []
            
        for item_list in keyword_array:
            if len(item_list) > 0:
                if ' ' in item_list:
                    keyword_item_array = item_list.split(' ')
                    is_match_all_exclude = True
                    for each_item in keyword_item_array:
                        each_item = format_keyword_string(each_item)
                        if not each_item in row_text:
                            is_match_all_exclude = False
                    if is_match_all_exclude:
                        is_match_keyword = True
                else:
                    item_list = format_keyword_string(item_list)
                    if item_list in row_text:
                        is_match_keyword = True
            else:
                is_match_keyword = True
            
            if is_match_keyword:
                break
    return is_match_keyword

def reset_row_text_if_match_keyword_exclude(config_dict, row_text):
    """如果匹配到排除關鍵字，則重置行文字 (視為不匹配)"""
    area_keyword_exclude = config_dict["keyword_exclude"]
    return is_row_match_keyword(area_keyword_exclude, row_text)

# ==========================================
# 網站特定邏輯 (TixCraft / KKTIX)
# ==========================================

def guess_tixcraft_question(driver, question_text):
    """猜測拓元 (TixCraft) 的驗證問題"""
    answer_list = []
    
    # 簡單的同意條款檢測
    inferred_answer_string = None
    if inferred_answer_string is None:
        if '輸入"YES"' in question_text and '同意' in question_text:
            inferred_answer_string = 'YES'

    if inferred_answer_string is None:
        if '驗證碼' in question_text and '輸入【同意】' in question_text:
            inferred_answer_string = '同意'

    if inferred_answer_string is None:
        if len(question_text) > 0:
            answer_list = get_answer_list_from_question_string(None, question_text)
    else:
        answer_list = [inferred_answer_string]

    return answer_list

def get_answer_list_from_user_guess_string(config_dict, CONST_MAXBOT_ANSWER_ONLINE_FILE):
    """獲取使用者自定義或線上的答案列表"""
    local_array = []
    online_array = []

    user_guess_string = config_dict["advanced"]["user_guess_string"]
    if len(user_guess_string) > 0:
        try:
            local_array = json.loads("[" + user_guess_string + "]")
        except Exception:
            local_array = []

    # 讀取線上題庫快取檔
    user_guess_string = ""
    if os.path.exists(CONST_MAXBOT_ANSWER_ONLINE_FILE):
        try:
            with open(CONST_MAXBOT_ANSWER_ONLINE_FILE, "r") as text_file:
                user_guess_string = text_file.readline()
        except Exception:
            pass

    if len(user_guess_string) > 0:
        try:
            online_array = json.loads("[" + user_guess_string + "]")
        except Exception:
            online_array = []

    return local_array + online_array

def check_answer_keep_symbol(captcha_text_div_text):
    """檢查是否需要保留答案中的符號 (大小寫、括號等)"""
    is_need_keep_symbol = False
    
    # 正規化文字以進行檢測
    keep_symbol_tmp = captcha_text_div_text.replace('也', '須').replace('必須', '須')
    keep_symbol_tmp = keep_symbol_tmp.replace('全都', '都').replace('全部都', '都')
    
    if '符號須都相同' in keep_symbol_tmp or '符號都相同' in keep_symbol_tmp:
        is_need_keep_symbol = True
        
    # 特殊規則：大小寫含括號需一模一樣
    keep_symbol_tmp = re.sub(r'(含|和|與|還有|及|以及|需|必須|而且|且|一模)', '', keep_symbol_tmp)
    if '大小寫括號相同' in keep_symbol_tmp:
        is_need_keep_symbol = True

    return is_need_keep_symbol

# ==========================================
# Discord 通知
# ==========================================

def build_discord_message(stage: str, platform_name: str) -> dict:
    """建構 Discord Webhook 訊息 Payload"""
    if not platform_name:
        platform_name = "Unknown"

    if stage == "ticket":
        message = f"[{platform_name}] found ticket! Please check your computer"
    elif stage == "order":
        message = f"[{platform_name}] order success! Please checkout and pay ASAP"
    else:
        message = f"[{platform_name}] notification"

    return {
        "content": message,
        "username": "Tickets Hunter"
    }

def send_discord_webhook(webhook_url: str, stage: str, platform_name: str, timeout: float = 3.0, verbose: bool = False) -> bool:
    """發送 Discord Webhook (同步)"""
    if not webhook_url:
        return False

    try:
        payload = build_discord_message(stage, platform_name)
        response = requests.post(
            webhook_url,
            json=payload,
            timeout=timeout
        )
        return response.status_code in (200, 204)
    except Exception as exc:
        if verbose:
            print(f"[Discord Webhook] Send failed: {exc}")
        return False

def send_discord_webhook_async(webhook_url: str, stage: str, platform_name: str, timeout: float = 3.0, verbose: bool = False) -> None:
    """發送 Discord Webhook (非同步)"""
    if not webhook_url:
        return

    thread = threading.Thread(
        target=send_discord_webhook,
        args=(webhook_url, stage, platform_name, timeout, verbose),
        daemon=True
    )
    thread.start()

# ==========================================
# Cloudflare Turnstile 驗證輔助
# ==========================================

def get_cf_template_paths() -> list:
    """獲取 Cloudflare Turnstile 模板圖片路徑列表"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    assets_dir = os.path.join(script_dir, "assets")

    template_files = [
        "cf_template_default.png",      # 預設模板
        "cf_template_ibon.png",         # ibon 專用
    ]

    templates = []
    for filename in template_files:
        path = os.path.join(assets_dir, filename)
        if os.path.exists(path):
            templates.append(path)

    return templates

async def verify_cf_with_templates(tab, templates: list = None, show_debug: bool = False) -> bool:
    """
    使用多個模板嘗試通過 Cloudflare Turnstile 驗證。
    使用 nodriver。
    """
    # 步驟 1: 嘗試內建模板 (英文版)
    try:
        if show_debug:
            print("[CF] Trying built-in template...")
        await tab.verify_cf(flash=show_debug)
        if show_debug:
            print("[CF] Success with built-in template")
        return True
    except Exception as exc:
        if show_debug:
            print(f"[CF] Built-in template failed: {exc}")

    # 步驟 2: 嘗試自定義模板
    if templates is None:
        templates = get_cf_template_paths()

    if not templates:
        return False

    for template_path in templates:
        try:
            template_name = os.path.basename(template_path)
            if show_debug:
                print(f"[CF] Trying template: {template_name}")

            await tab.verify_cf(template_image=template_path, flash=show_debug)
            
            return True
        except Exception:
            continue

    return False