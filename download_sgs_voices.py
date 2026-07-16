import pandas as pd
import requests
import re
import os
import time
import random
from urllib.parse import quote
from pathlib import Path

def download_sgs_voices(excel_path, output_dir="sgs_voices"):
    """
    从三国杀Wiki下载武将台词语音
    
    参数:
        excel_path: Excel文件路径（三国杀.xlsx，"全部"工作表包含武将名列表）
        output_dir: 语音输出目录（插件运行时从此目录读取，默认为 sgs_voices）
    """
    
    # 设置请求头模拟真人浏览器
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }
    
    # 创建会话以保持连接
    session = requests.Session()
    session.headers.update(headers)
    
    # 创建输出目录
    output_base = Path(output_dir)
    output_base.mkdir(exist_ok=True)
    
    # 读取Excel文件（"全部"工作表包含武将名列表）
    print(f"正在读取Excel文件: {excel_path}")
    df = pd.read_excel(excel_path, sheet_name="全部", header=None)
    
    # 获取B列从第2行开始的武将名称（索引从0开始，所以第2行是索引1）
    heroes = df.iloc[1:, 1].dropna().astype(str).tolist()
    print(f"共找到 {len(heroes)} 个武将")
    
    # 创建记录文件用于断点续传
    record_file = output_base / 'download_record.txt'
    downloaded_heroes = set()
    
    if record_file.exists():
        with open(record_file, 'r', encoding='utf-8') as f:
            downloaded_heroes = set(line.strip() for line in f if line.strip())
        print(f"发现记录文件，已跳过 {len(downloaded_heroes)} 个已下载武将")
    
    # 遍历每个武将
    for idx, hero in enumerate(heroes, 1):
        hero = hero.strip()
        
        # 跳过已下载的
        if hero in downloaded_heroes:
            print(f"[{idx}/{len(heroes)}] 跳过已下载: {hero}")
            continue
        
        print(f"\n[{idx}/{len(heroes)}] 正在处理: {hero}")
        
        # 创建武将文件夹（在输出目录下）
        hero_folder = output_base / hero
        hero_folder.mkdir(exist_ok=True)
        
        # 构建URL（需要进行URL编码）
        encoded_name = quote(hero, safe='')
        url = f"https://wiki.biligame.com/sgsol/{encoded_name}"
        
        try:
            # 增加初始延迟：每个武将页面访问前等待 3-6 秒
            time.sleep(random.uniform(3, 6))
            
            # 获取网页内容
            response = session.get(url, timeout=30)
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                print(f"  ⚠️ 无法访问页面，状态码: {response.status_code}")
                # 遇到错误增加额外冷却时间
                time.sleep(random.uniform(10, 15))
                continue
            
            html_content = response.text
            
            # --- 改进的音频和文本提取逻辑 ---
            matches = []
            
            # 方法：查找所有包含 data-src 的 span 标签 (音频标签)
            span_pattern = r'<span[^>]*class="[^"]*bikit-audio[^"]*"[^>]*data-src="([^"]+)"[^>]*>'
            
            potential_matches = []
            for match in re.finditer(span_pattern, html_content):
                audio_url = match.group(1).replace('&#58;', ':').replace('&amp;', '&')
                span_start = match.start()
                
                # 向前查找文本
                search_range_start = max(0, span_start - 200)
                context_before = html_content[search_range_start:span_start]
                
                # 获取 context_before 中最后一个 '>' 之后的内容
                last_tag_end = context_before.rfind('>')
                if last_tag_end != -1:
                    raw_text = context_before[last_tag_end+1:]
                else:
                    raw_text = context_before
                
                # 清洗文本：去除 HTML 标签、多余空白、换行
                clean_text = re.sub(r'<[^>]+>', '', raw_text).strip()
                clean_text = re.sub(r'\s+', ' ', clean_text)
                
                if clean_text:
                    potential_matches.append((clean_text, audio_url))
                else:
                    potential_matches.append((f"音频_{len(potential_matches)+1}", audio_url))

            matches = potential_matches
            
            if not matches:
                # 尝试更宽松的匹配
                pattern2 = r'>([^<]+)<span[^>]*class="[^"]*bikit-audio[^"]*"[^>]*data-src="([^"]+)"'
                matches = re.findall(pattern2, html_content)
            
            if not matches:
                print(f"  ⚠️ 未找到音频资源")
                # 记录已完成（即使没有音频也标记为处理过，避免重复尝试）
                with open(record_file, 'a', encoding='utf-8') as f:
                    f.write(f"{hero}\n")
                continue
            
            print(f"  找到 {len(matches)} 个音频")
            
            # 获取该文件夹下所有现有文件名（用于检测重复）
            existing_files = list(hero_folder.glob('*.mp3'))
            existing_filenames = [f.name for f in existing_files]
            
            # 下载每个音频
            audio_links = []
            skipped_count = 0
            downloaded_count = 0
            
            for audio_idx, (text, audio_url) in enumerate(matches, 1):
                # 清理文本和URL
                text = text.strip()
                audio_url = audio_url.replace('&#58;', ':').replace('&amp;', '&')
                
                # 生成安全文本（用于检测重复）
                safe_text = re.sub(r'[\\/:*?"<>|]', '_', text)
                if len(safe_text) > 50:
                    safe_text = safe_text[:50]
                
                # --- 改进的重复检测逻辑：检查是否已有文件包含 safe_text ---
                is_duplicate = False
                for existing_name in existing_filenames:
                    # 移除序号前缀（如 "01_"）后检查是否包含 safe_text
                    # 或者检查文件名是否包含 safe_text
                    if safe_text in existing_name:
                        is_duplicate = True
                        break
                
                if is_duplicate:
                    print(f"    [{audio_idx}/{len(matches)}] 跳过重复: {safe_text[:30]}...")
                    skipped_count += 1
                    # 仍然记录链接信息到Excel
                    # 尝试从现有文件名中找到匹配的文件
                    matching_file = None
                    for existing_name in existing_filenames:
                        if safe_text in existing_name:
                            matching_file = existing_name
                            break
                    
                    audio_links.append({
                        'text': text,
                        'url': audio_url,
                        'filename': matching_file or f"已存在_{safe_text}.mp3"
                    })
                    continue
                
                # 生成完整文件名（带序号）
                filename = f"{audio_idx:02d}_{safe_text}.mp3"
                filepath = hero_folder / filename
                
                try:
                    # 增加下载间隔：每次下载前等待 2-5 秒
                    time.sleep(random.uniform(2, 5))
                    
                    audio_response = session.get(audio_url, timeout=30, stream=True)
                    
                    if audio_response.status_code == 200:
                        with open(filepath, 'wb') as f:
                            for chunk in audio_response.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        print(f"    [{audio_idx}/{len(matches)}] ✓ 下载成功: {filename[:50]}...")
                        audio_links.append({
                            'text': text,
                            'url': audio_url,
                            'filename': filename
                        })
                        downloaded_count += 1
                        # 更新现有文件列表
                        existing_filenames.append(filename)
                    else:
                        print(f"    [{audio_idx}/{len(matches)}] ✗ 下载失败: {audio_response.status_code}")
                        # 下载失败增加冷却
                        time.sleep(random.uniform(5, 8))
                        
                except Exception as e:
                    print(f"    [{audio_idx}/{len(matches)}] ✗ 下载错误: {str(e)}")
                    # 错误后增加冷却
                    time.sleep(random.uniform(5, 8))
            
            print(f"  下载完成: {downloaded_count} 个新文件, 跳过 {skipped_count} 个重复文件")
            
            # 将超链接写入Excel（在当前武将行）
            if audio_links:
                # 构建超链接字符串 (Excel HYPERLINK格式)
                links_text = []
                for link in audio_links:
                    excel_link = f'=HYPERLINK("{link["url"]}","{link["text"]}")'
                    links_text.append(excel_link)
                
                # 将链接写入C列（Excel行索引 = DataFrame索引 + 1，因为第1行是标题）
                excel_row_idx = idx  # idx 从1开始，对应Excel第2行（索引1）
                if excel_row_idx < len(df):
                    df.iloc[excel_row_idx, 2] = '\n'.join(links_text)
                print(f"  已写入 {len(audio_links)} 个超链接到Excel")
            
            # 记录已完成的武将
            with open(record_file, 'a', encoding='utf-8') as f:
                f.write(f"{hero}\n")
            
            # 每处理3个武将保存一次Excel（更频繁保存），并增加冷却
            if idx % 3 == 0:
                output_path = excel_path.replace('.xlsx', '_with_links.xlsx')
                df.to_excel(output_path, index=False, header=False)
                print(f"  已保存进度到: {output_path}")
                # 每3个武将后额外冷却 5-10 秒，降低被封风险
                print(f"  ⏱️ 冷却中...")
                time.sleep(random.uniform(5, 10))
                
        except Exception as e:
            print(f"  ✗ 处理出错: {str(e)}")
            # 遇到错误时等待更长时间
            time.sleep(random.uniform(15, 25))
            continue
    
    # 最终保存
    output_path = excel_path.replace('.xlsx', '_with_links.xlsx')
    df.to_excel(output_path, index=False, header=False)
    print(f"\n✅ 全部完成！结果已保存到: {output_path}")
    print(f"📁 音频文件保存在: {output_base}/")

# 主程序
if __name__ == "__main__":
    # 使用方法:
    #   1. 安装依赖: pip install pandas openpyxl requests
    #   2. 确保 三国杀.xlsx 在当前目录
    #   3. 运行: python download_sgs_voices.py
    #   4. 语音文件将下载到 sgs_voices/ 目录
    #   5. 支持断点续传，中断后重新运行会自动跳过已下载的武将
    excel_file = "三国杀.xlsx"
    download_sgs_voices(excel_file, output_dir="sgs_voices")
