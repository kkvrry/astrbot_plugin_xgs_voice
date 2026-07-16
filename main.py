import os
import random
import re
import difflib
import subprocess
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig, logger
from astrbot.api.star import StarTools
import astrbot.api.message_components as Comp

# 默认 LLM 唤醒词列表（模块级常量）
DEFAULT_LLM_PATTERNS = [
    r'[?？]',
    r'怎么',
    r'如何',
    r'为什么',
    r'什么是',
    r'是什么',
    r'能不能',
    r'可不可以',
    r'帮我',
    r'请问',
    r'告诉我',
    r'解释',
    r'说说',
    r'介绍',
    r'推荐',
    r'建议',
    r'分析',
    r'总结',
    r'翻译',
    r'写一',
    r'帮忙',
    r'教我',
    r'怎样',
    r'哪个',
    r'哪些',
    r'多少',
    r'几个',
    r'有没有',
    r'是否',
    r'能否',
    r'可以吗',
    r'行吗',
    r'好吗',
    r'对吗',
    r'吗$',
]

class VoiceManager:
    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.hero_map = {}  # Hero -> List[full_path] (sorted)
        self.keyword_map = []  # List[(keyword, full_path)]
        self.last_played = {}  # Hero -> last_file_path (for random selection)
        self._sorted_heroes = []  # 按长度降序排列的英雄名缓存
        self._all_files_cache = []  # 所有音频的扁平列表缓存
        self.scan()

    def scan(self):
        """扫描音频目录，构建索引"""
        self.hero_map = {}
        self.keyword_map = []

        if not os.path.exists(self.root_dir):
            logger.error(f"[三国杀语音] 音频目录不存在: {self.root_dir}")
            return

        _digit_re = re.compile(r'^(\d+)')  # 预编译，避免排序时重复编译

        for hero_name in os.listdir(self.root_dir):
            hero_path = os.path.join(self.root_dir, hero_name)
            if not os.path.isdir(hero_path):
                continue

            # 获取该英雄的所有音频文件并排序
            files = [f for f in os.listdir(hero_path) if f.lower().endswith(('.mp3', '.wav'))]
            try:
                files.sort(key=lambda x: int(_digit_re.match(x).group(1)) if _digit_re.match(x) else 999)
            except Exception:
                files.sort()

            full_paths = [os.path.join(hero_path, f) for f in files]
            if full_paths:
                self.hero_map[hero_name] = full_paths

            # 构建关键词映射
            for f in files:
                name_without_ext = os.path.splitext(f)[0]
                match = re.match(r'^(\d+)[_.\-\s]+(.+)$', name_without_ext)
                if match:
                    keyword = match.group(2)
                else:
                    keyword = name_without_ext

                if keyword:
                    self.keyword_map.append((keyword, os.path.join(hero_path, f)))

        # 重建缓存
        self._sorted_heroes = sorted(self.hero_map.keys(), key=len, reverse=True)
        self._all_files_cache = [
            (f, hero) for hero, files in self.hero_map.items() for f in files
        ]

        logger.info(f"[三国杀语音] 扫描完成: 发现 {len(self.hero_map)} 个英雄，{len(self.keyword_map)} 个关键词")

    def match_hero(self, message: str):
        """
        匹配英雄名+序号模式
        返回: (audio_path_or_list, is_random, is_all) 或 None
        """
        for hero in self._sorted_heroes:
            # 检查消息是否以英雄名开头
            if message.lower().startswith(hero.lower()):
                suffix = message[len(hero):].strip()

                # 情况1: 只有英雄名 -> 随机播放
                if not suffix:
                    return self._get_random_audio(hero), True, False

                # 情况2: 英雄名+0 -> 发送所有语音
                if suffix == "0":
                    return self._get_all_audio(hero), False, True

                # 情况3: 英雄名+数字 -> 指定播放
                if suffix.isdigit():
                    idx = int(suffix)
                    return self._get_indexed_audio(hero, idx), False, False

        return None

    def _get_random_audio(self, hero: str):
        """获取随机音频，避免连续重复"""
        files = self.hero_map.get(hero, [])
        if not files:
            return None

        if len(files) == 1:
            return files[0]

        last = self.last_played.get(hero)
        # 尝试随机选择一个与上次不同的
        candidates = [f for f in files if f != last]
        if not candidates: # 理论上不会发生，除非只有1个文件且上面已处理
            candidates = files

        selected = random.choice(candidates)
        self.last_played[hero] = selected
        return selected

    def _get_indexed_audio(self, hero: str, index: int):
        """获取指定序号的音频"""
        files = self.hero_map.get(hero, [])
        if not files:
            return None

        # 序号从1开始
        real_index = index - 1

        if real_index < 0: # 序号0或负数，默认第一个
            return files[0]

        if real_index >= len(files):
            return files[-1] # 超出范围，返回最后一个

        return files[real_index]

    def _get_all_audio(self, hero: str):
        """获取英雄的所有音频文件列表"""
        return self.hero_map.get(hero, [])

    def get_random_voices(self, count: int):
        """从所有英雄中随机选取指定数量的语音，返回 [(path, hero_name), ...]"""
        if not self._all_files_cache:
            return []
        count = min(count, len(self._all_files_cache))
        return random.sample(self._all_files_cache, count)

    def match_keyword(self, message: str, fuzzy_threshold=0.6):
        """
        匹配关键词（支持模糊匹配）
        返回: (audio_path, keyword) 或 None
        """
        message = message.lower()

        # 1. 精确匹配 (双向包含)
        # 优先: 消息包含关键词 (Trigger)
        for keyword, path in self.keyword_map:
            if keyword.lower() in message:
                return path, keyword

        # 次优: 关键词包含消息 (用户只说了片段，且片段长度足够)
        if len(message) >= 2:
            for keyword, path in self.keyword_map:
                if message in keyword.lower():
                    return path, keyword

        # 2. 模糊匹配 (基于相似度)
        # 仅当消息长度适中时尝试，避免对极短或极长消息进行昂贵计算
        if 2 <= len(message) <= 20:
            best_ratio = 0
            best_match = None

            for keyword, path in self.keyword_map:
                # 计算相似度
                ratio = difflib.SequenceMatcher(None, message, keyword.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = (path, keyword)

            if best_ratio >= fuzzy_threshold and best_match:
                return best_match

        return None

@register("astrbot_plugin_sgsvoice", "落日七号、复读机长", "三国杀自动玩梗语音插件 - 识别聊天中的三国杀经典台词关键词，自动发送对应语音", "1.4.0", "https://github.com/kvrry/astrbot_plugin_xgs_voice")
class SgsVoiceMeme(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.base_dir = os.path.dirname(__file__)
        # 优先检查插件目录下的 sgs_voices，兼容旧版 data/sound 目录
        self.audio_dir = os.path.join(self.base_dir, "sgs_voices")
        if not os.path.exists(self.audio_dir):
            self.audio_dir = os.path.join(self.base_dir, "data", "sound")

        # 初始化语音管理器
        self.voice_manager = VoiceManager(self.audio_dir)

        # 统计信息
        self.trigger_count = 0

        # 加载持久化数据目录
        self.data_dir = StarTools.get_data_dir("sgsvoice")

        # 英雄列表图片缓存路径
        self._hero_list_img_path = os.path.join(self.data_dir, "hero_list_cache.png")
        self._hero_list_count = 0  # 缓存时的英雄数量

        # 加载配置
        self._load_config()

        logger.info(f"[三国杀语音] 插件初始化完成！")
        logger.info(f"[三国杀语音] 音频目录: {self.audio_dir}")
        logger.info(f"[三国杀语音] 数据目录: {self.data_dir}")

    def _get_wav_path(self, audio_path: str) -> str:
        """
        获取音频的 WAV 版本路径。如果是 MP3，则转换为 WAV。
        用于兼容某些只支持 WAV 的平台（如 qq_official）。
        """
        if audio_path.lower().endswith(".wav"):
            return audio_path

        # 在数据目录下创建缓存文件夹
        cache_dir = os.path.join(self.data_dir, "cache_wav")

        # 计算相对于音频根目录的相对路径，以保持缓存目录结构
        try:
            rel_path = os.path.relpath(audio_path, self.audio_dir)
        except ValueError:
            # 如果不在根目录下（理论上不应该），使用文件名
            rel_path = os.path.basename(audio_path)

        wav_rel_path = os.path.splitext(rel_path)[0] + ".wav"
        wav_path = os.path.join(cache_dir, wav_rel_path)

        # 如果缓存已存在，直接返回
        if os.path.exists(wav_path):
            return wav_path

        # 否则进行转换
        os.makedirs(os.path.dirname(wav_path), exist_ok=True)
        try:
            # 使用 ffmpeg 转换为 WAV (pcm_s16le, 16000Hz, mono 这种格式最通用)
            # 如果不确定格式，直接简单转换也可
            cmd = [
                "ffmpeg", "-y", "-i", audio_path,
                "-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000",
                wav_path, "-loglevel", "quiet"
            ]
            subprocess.run(cmd, check=True)
            return wav_path
        except Exception as e:
            logger.error(f"[三国杀语音] 音频转换失败 (MP3 -> WAV): {e}")
            # 如果转换失败，返回原路径，尝试让适配器自行处理
            return audio_path

    def _extract_voice_text(self, audio_path: str) -> str:
        """从文件名提取台语文本: 01_台词.mp3 -> 台词"""
        filename = os.path.basename(audio_path)
        name_without_ext = os.path.splitext(filename)[0]
        match = re.match(r'^\d+[_.\-\s]+(.+)$', name_without_ext)
        return match.group(1) if match else name_without_ext

    def _merge_audio_files(self, audio_paths: list) -> str:
        """将多个音频文件合并为一个 MP3，片段间插入 0.6s 静音间隔"""
        import hashlib

        cache_dir = os.path.join(self.data_dir, "cache_merged")
        os.makedirs(cache_dir, exist_ok=True)

        # 用路径列表的哈希值做缓存文件名
        key = hashlib.md5('|'.join(sorted(audio_paths)).encode()).hexdigest()
        merged_path = os.path.join(cache_dir, f"{key}.mp3")

        if os.path.exists(merged_path):
            return merged_path

        # 构建 ffmpeg concat filter：[0:a][silence][1:a][silence]...concat=n:out_type=a
        inputs = []
        filter_parts = []
        stream_labels = []

        for i, path in enumerate(audio_paths):
            inputs.extend(["-i", path])
            if i > 0:
                # 在前一个片段后插入 0.6s 静音
                silence_label = f"[s{i}]"
                filter_parts.append(
                    f"anullsrc=r=44100:cl=mono:d=0.6{silence_label}"
                )
                stream_labels.append(silence_label)
            stream_labels.append(f"[{i}:a]")

        concat_n = len(stream_labels)
        filter_parts.append(
            f"{''.join(stream_labels)}concat=n={concat_n}:v=0:a=1[out]"
        )

        filter_str = ';'.join(filter_parts)

        try:
            cmd = [
                "ffmpeg", "-y",
                *inputs,
                "-filter_complex", filter_str,
                "-map", "[out]",
                "-acodec", "libmp3lame", "-ab", "128k",
                "-t", "300",  # 最长 5 分钟
                merged_path,
                "-loglevel", "quiet"
            ]
            subprocess.run(cmd, check=True, timeout=120)
            return merged_path
        except Exception as e:
            logger.error(f"[三国杀语音] 音频合并失败: {e}")
            return None

    def _load_config(self):
        """从配置对象加载设置"""
        self.need_llm_patterns = self.config.get("llm_wake_patterns", DEFAULT_LLM_PATTERNS)
        self.wake_word_prefix = self.config.get("wake_word_prefix", "/")
        self.require_prefix = self.config.get("require_prefix", False)
        self.enable_group_only = self.config.get("enable_group_only", True)
        self.private_chat_llm_mode = self.config.get("private_chat_llm_mode", "smart")
        self.fuzzy_threshold = self.config.get("fuzzy_threshold", 0.6)

    def _needs_llm_response(self, message: str, event: AstrMessageEvent) -> bool:
        """判断消息是否需要 LLM 回复"""
        clean_message = message.strip()
        is_private = not event.message_obj.group_id

        is_wake_word = clean_message.startswith(self.wake_word_prefix)

        is_at_bot = False
        bot_id = str(event.message_obj.self_id)
        for comp in event.message_obj.message:
            if isinstance(comp, Comp.At):
                qq_id = getattr(comp, 'qq', None) or getattr(comp, 'target', None)
                if qq_id and str(qq_id) == bot_id:
                    is_at_bot = True
                    break

        if is_wake_word:
            return False

        if is_private:
            if self.private_chat_llm_mode == "always": return True
            elif self.private_chat_llm_mode == "never": return False

        if not is_private and not is_at_bot:
            return False

        for pattern in self.need_llm_patterns:
            if re.search(pattern, clean_message):
                return True

        # 简单判断：如果消息很短且触发了语音，可能不需要 LLM
        if len(clean_message) < 5:
             return False

        return True

    def _generate_hero_list_image(self):
        """生成英雄列表精美图片，带缓存"""
        hero_count = len(self.voice_manager.hero_map)

        # 检查缓存是否有效
        if (os.path.exists(self._hero_list_img_path)
                and self._hero_list_count == hero_count
                and hero_count > 0):
            return self._hero_list_img_path

        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            logger.error("[三国杀语音] 生成英雄列表图片需要 Pillow 库，请安装: pip install Pillow")
            return None

        # 按拼音首字母排序
        try:
            from pypinyin import lazy_pinyin
            heroes = sorted(self.voice_manager.hero_map.keys(), key=lambda h: lazy_pinyin(h))
        except ImportError:
            import locale
            try:
                heroes = sorted(self.voice_manager.hero_map.keys(), key=locale.strxfrm)
            except Exception:
                heroes = sorted(self.voice_manager.hero_map.keys())
        if not heroes:
            return None

        # ---- 布局参数 ----
        cols = 8
        rows_count = -(-len(heroes) // cols)  # 向上取整

        card_w = 128
        card_h = 42
        gap_x = 8
        gap_y = 6
        pad_x = 48
        pad_top = 110
        pad_bottom = 50

        img_w = pad_x * 2 + cols * card_w + (cols - 1) * gap_x
        img_h = pad_top + rows_count * (card_h + gap_y) + pad_bottom

        # ---- 创建画布 ----
        img = Image.new('RGB', (img_w, img_h), '#121214')
        draw = ImageDraw.Draw(img)

        # ---- 加载字体 ----
        def _try_fonts(names, size):
            for name in names:
                try:
                    return ImageFont.truetype(name, size)
                except Exception:
                    continue
            return ImageFont.load_default()

        font_title = _try_fonts(['msyh.ttc', 'msyhbd.ttc', 'simhei.ttf', 'simsun.ttc'], 36)
        font_sub = _try_fonts(['msyh.ttc', 'simhei.ttf'], 16)
        font_name = _try_fonts(['msyh.ttc', 'simhei.ttf', 'simsun.ttc'], 17)
        font_count = _try_fonts(['msyh.ttc', 'simhei.ttf'], 11)

        # ---- 绘制标题区域 ----
        title = "三国杀 · 英雄列表"
        draw.text((img_w // 2, 35), title, fill='#FFFFFF', font=font_title, anchor='mt')

        # 装饰线
        line_w = 200
        lx = (img_w - line_w) // 2
        draw.line([(lx, 72), (lx + line_w, 72)], fill='#3A3A42', width=2)
        # 装饰线两端点缀
        draw.ellipse([(lx - 3, 69), (lx + 3, 75)], fill='#E62828')
        draw.ellipse([(lx + line_w - 3, 69), (lx + line_w + 3, 75)], fill='#E62828')

        subtitle = f"共收录 {hero_count} 位英雄"
        draw.text((img_w // 2, 85), subtitle, fill='#9999A2', font=font_sub, anchor='mt')

        # ---- 绘制英雄卡片网格 ----
        for i, hero in enumerate(heroes):
            col = i % cols
            row = i // cols

            x = pad_x + col * (card_w + gap_x)
            y = pad_top + row * (card_h + gap_y)

            # 卡片背景
            draw.rounded_rectangle(
                [(x, y), (x + card_w, y + card_h)],
                radius=6, fill='#1A1A1E', outline='#2A2A30'
            )
            # 顶部微高光
            draw.line(
                [(x + 8, y + 1), (x + card_w - 8, y + 1)],
                fill='#2E2E36', width=1
            )

            # 英雄名（居中，超长名称自动截断）
            voice_count = len(self.voice_manager.hero_map[hero])
            display_name = hero
            bbox = draw.textbbox((0, 0), display_name, font=font_name)
            text_w = bbox[2] - bbox[0]
            while text_w > card_w - 12 and len(display_name) > 1:
                display_name = display_name[:-1]
                bbox = draw.textbbox((0, 0), display_name + '…', font=font_name)
                text_w = bbox[2] - bbox[0]
            if display_name != hero:
                display_name += '…'
            draw.text(
                (x + card_w // 2, y + card_h // 2 - 3),
                display_name, fill='#FFFFFF', font=font_name, anchor='mm'
            )

            # 语音条数标记
            count_text = f"{voice_count}条"
            draw.text(
                (x + card_w // 2, y + card_h - 7),
                count_text, fill='#666670', font=font_count, anchor='mm'
            )

        # ---- 底部信息 ----
        footer = "发送「英雄名0」听全部语音  ·  发送「随机台词N」随机N条"
        draw.text((img_w // 2, img_h - 20), footer, fill='#555560', font=font_count, anchor='mm')

        # ---- 保存 ----
        os.makedirs(os.path.dirname(self._hero_list_img_path), exist_ok=True)
        img.save(self._hero_list_img_path, quality=95)
        self._hero_list_count = hero_count
        logger.info(f"[三国杀语音] 英雄列表图片已生成: {self._hero_list_img_path}")
        return self._hero_list_img_path

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息"""
        message = event.message_str.strip()
        if not message:
            return

        is_private = not event.message_obj.group_id
        if self.enable_group_only and is_private:
            return

        # 前缀触发逻辑
        if self.require_prefix:
            if not message.startswith(self.wake_word_prefix):
                return
            message = message[len(self.wake_word_prefix):].strip()
            if not message:
                return

        voice_infos = []  # [(hero_name, voice_text, path), ...]
        trigger_keyword = ""

        # ---- 功能: 随机台词+数字 ----
        random_match = re.match(r'^随机台词\s*(\d+)$', message)
        if random_match:
            count = min(int(random_match.group(1)), 5)
            results = self.voice_manager.get_random_voices(count)
            if results:
                for path, hero in results:
                    text = self._extract_voice_text(path)
                    voice_infos.append((hero, text, path))
                trigger_keyword = f"随机台词 {count}条"

        # ---- 功能: 英雄列表 ----
        elif message == "英雄列表":
            img_path = self._generate_hero_list_image()
            if img_path:
                logger.info("[三国杀语音] 发送英雄列表图片")
                self.trigger_count += 1
                try:
                    yield event.chain_result([
                        Comp.Image(file=img_path)
                    ])
                except Exception as e:
                    logger.error(f"[三国杀语音] 发送英雄列表图片失败: {e}")
                    yield event.plain_result(f"英雄列表图片发送失败，请检查日志。")
                event.stop_event()
                return

        # ---- 原有: 英雄名+序号 ----
        else:
            hero_match = self.voice_manager.match_hero(message)
            if hero_match:
                result, is_random, is_all = hero_match
                if is_all:
                    # 英雄名+0：全部语音
                    hero_name = message.rstrip('0').strip()
                    # 从路径中获取实际英雄名（匹配后的标准名）
                    if result:
                        hero_name = os.path.basename(os.path.dirname(result[0]))
                    for path in result:
                        text = self._extract_voice_text(path)
                        voice_infos.append((hero_name, text, path))
                    trigger_keyword = f"{hero_name} 全部语音（共{len(result)}条）"
                else:
                    # 英雄名+序号：单条语音
                    path = result
                    hero_name = os.path.basename(os.path.dirname(path))
                    text = self._extract_voice_text(path)
                    voice_infos.append((hero_name, text, path))
                    trigger_keyword = f"{hero_name}: {text}"
            else:
                # ---- 原有: 关键词模糊匹配 ----
                keyword_match = self.voice_manager.match_keyword(message, self.fuzzy_threshold)
                if keyword_match:
                    path, kw = keyword_match
                    hero_name = os.path.basename(os.path.dirname(path))
                    voice_infos.append((hero_name, kw, path))
                    trigger_keyword = kw

        if not voice_infos:
            return

        # 过滤无效路径
        voice_infos = [(h, t, p) for h, t, p in voice_infos if p and os.path.exists(p)]
        if not voice_infos:
            return

        n = len(voice_infos)
        logger.info(f"[三国杀语音] 触发: '{trigger_keyword}', 发送 {n} 条语音")
        self.trigger_count += 1

        # ---- 多条语音：提示所有台词 + 5条限制 + 音频合并 ----
        if n > 1:
            need_merge = n > 4
            individual = voice_infos[:3] if need_merge else voice_infos
            merged = voice_infos[3:] if need_merge else []

            # 构建提示文本：列出所有语音的英雄名+台词
            lines = [f"🎭 {trigger_keyword}"]
            for i, (hero, text, _) in enumerate(voice_infos, 1):
                lines.append(f"{i}. 【{hero}】{text}")
            if need_merge:
                lines.append(f"（第4~{n}条已合并为一条音频）")

            try:
                yield event.plain_result('\n'.join(lines))
                await asyncio.sleep(0.3)
            except Exception:
                pass

            # 逐条发送单独语音（前3条）
            for hero, text, path in individual:
                final_audio_path = self._get_wav_path(path)
                try:
                    yield event.chain_result([
                        Comp.Record(file=final_audio_path, url=final_audio_path)
                    ])
                    await asyncio.sleep(0.6)
                except Exception as e:
                    logger.error(f"[三国杀语音] 发送语音失败: {e}")

            # 多余语音合并为一条音频发送
            if merged:
                merge_paths = [p for _, _, p in merged]
                merged_audio = self._merge_audio_files(merge_paths)
                if merged_audio and os.path.exists(merged_audio):
                    try:
                        yield event.chain_result([
                            Comp.Record(file=merged_audio, url=merged_audio)
                        ])
                    except Exception as e:
                        logger.error(f"[三国杀语音] 发送合并语音失败: {e}")
                else:
                    # 合并失败，逐条发送（可能超出5条限制）
                    for hero, text, path in merged:
                        final_audio_path = self._get_wav_path(path)
                        try:
                            yield event.chain_result([
                                Comp.Record(file=final_audio_path, url=final_audio_path)
                            ])
                            await asyncio.sleep(0.6)
                        except Exception as e:
                            logger.error(f"[三国杀语音] 发送语音失败: {e}")
        else:
            # 单条语音直接发送
            _, _, path = voice_infos[0]
            final_audio_path = self._get_wav_path(path)
            try:
                yield event.chain_result([
                    Comp.Record(file=final_audio_path, url=final_audio_path)
                ])
            except Exception as e:
                logger.error(f"[三国杀语音] 发送语音失败: {e}")

        # 智能判断是否需要 LLM 回复
        if self._needs_llm_response(message, event):
            yield event.request_llm(prompt=message)
        else:
            event.stop_event()

    @filter.command_group("sgs")
    def sgs_group(self):
        pass

    @sgs_group.command("help")
    async def sgs_help(self, event: AstrMessageEvent):
        prefix_mode = f"前缀触发（{self.wake_word_prefix}）" if self.require_prefix else "自由触发"
        help_text = f"""🎭 三国杀语音插件 v1.4

📌 功能：
1. 「英雄名+序号」点播语音（如：SP关羽3）
2. 「英雄名+0」发送该英雄全部语音（如：曹操0）
3. 英雄名不带序号随机播放（不重复）
4. 「随机台词N」随机发送N条语音（上限5条，如：随机台词3）
5. 「英雄列表」查看所有英雄精美图片
6. 关键词模糊匹配（相似度阈值: {self.fuzzy_threshold*100:.0f}%）

当前状态：
• 触发模式: {prefix_mode}
• 英雄数量: {len(self.voice_manager.hero_map)}
• 关键词数: {len(self.voice_manager.keyword_map)}

可用指令：
• /sgs help - 帮助
• /sgs hero_list - 英雄列表图片
• /sgs stats - 统计
• /sgs reload - 重载配置和音频
"""
        yield event.plain_result(help_text)

    @sgs_group.command("hero_list")
    async def sgs_hero_list(self, event: AstrMessageEvent):
        """生成并发送英雄列表图片"""
        img_path = self._generate_hero_list_image()
        if img_path:
            try:
                yield event.chain_result([
                    Comp.Image(file=img_path)
                ])
            except Exception as e:
                yield event.plain_result(f"❌ 图片发送失败: {e}")
        else:
            yield event.plain_result("❌ 英雄列表图片生成失败，请检查日志或安装 Pillow 库。")

    @sgs_group.command("stats")
    async def sgs_stats(self, event: AstrMessageEvent):
        hero_count = len(self.voice_manager.hero_map)
        keyword_count = len(self.voice_manager.keyword_map)

        stats_text = f"""📊 统计信息
本次触发: {self.trigger_count} 次
收录英雄: {hero_count} 位
收录关键词: {keyword_count} 条
"""
        yield event.plain_result(stats_text)

    @sgs_group.command("reload")
    async def sgs_reload(self, event: AstrMessageEvent):
        """重新加载配置和音频文件"""
        # 1. 重新加载配置
        self._load_config()
        # 2. 重新扫描音频
        self.voice_manager.scan()
        # 3. 清除英雄列表图片缓存
        self._hero_list_count = 0

        yield event.plain_result(f"✅ 已重载配置并扫描音频目录！\n英雄: {len(self.voice_manager.hero_map)}\n关键词: {len(self.voice_manager.keyword_map)}\n触发模式: {'前缀触发' if self.require_prefix else '自由触发'}\n模糊匹配阈值: {self.fuzzy_threshold*100:.0f}%")
