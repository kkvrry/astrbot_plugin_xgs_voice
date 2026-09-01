import os
import random
import re
import time
import hashlib
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
    # 支持的音频格式（m4a 同样支持，发送前会经 ffmpeg 转码为 WAV）
    AUDIO_EXTS = ('.mp3', '.wav', '.m4a')

    def __init__(self, base_dir, audio_root="", default_lib="", min_keyword_len=2, extra_libs=None):
        """
        base_dir: 插件目录。
        audio_root: 配置指定的语音库大目录（绝对路径或相对插件目录）；留空则自动检测 voice/。
                   默认结构: 插件目录/voice/ 下每个一级文件夹是一个语音库。
        default_lib: 默认语音库目录（兜底），默认 voice/sgs_voices。
        min_keyword_len: 关键词最短长度，过滤过短的词避免误触发（默认 2）。
        extra_libs: 额外语音库目录列表，每项可为一个库目录（其下为角色文件夹）
                   或含多个库的大目录（绝对路径或相对插件目录），在自动识别之外追加。
        """
        self.base_dir = base_dir
        self.audio_root = audio_root
        self.default_lib = default_lib
        self.min_keyword_len = max(1, int(min_keyword_len or 2))
        self.extra_libs = [str(x).strip() for x in (extra_libs or []) if str(x).strip()]
        self.categories = {}  # 语音库名 -> {角色名: [音频路径]}（dict 保持插入序）
        self.category_order = []  # 语音库名有序列表
        self.role_map = {}  # 角色名 -> [音频路径]（跨库展平索引）
        self.role_category = {}  # 角色名 -> 语音库名
        self.file_to_role = {}  # 音频路径 -> (语音库名, 角色名)
        self.keyword_map = []  # List[(keyword, full_path)]
        self._bigram_index = {}  # bigram -> [keyword_map 下标]（模糊匹配粗筛）
        self.last_played = {}  # Hero -> last_file_path (for random selection)
        self._sorted_roles = []  # 按长度降序排列的角色名缓存
        self._all_files_cache = []  # 所有音频的扁平列表缓存
        self.lib_signature = ""  # 音频库内容签名（用于列表图片缓存失效判断）
        self.scan()

    @staticmethod
    def _is_audio(fname: str) -> bool:
        return fname.lower().endswith(VoiceManager.AUDIO_EXTS)

    @staticmethod
    def _sort_files(files) -> list:
        """按文件名数字前缀排序（无数字前缀的排后面）"""
        _digit_re = re.compile(r'^(\d+)')
        try:
            return sorted(
                files,
                key=lambda x: int(_digit_re.match(x).group(1)) if _digit_re.match(x) else 999
            )
        except Exception:
            return sorted(files)

    @staticmethod
    def _bigrams(s: str) -> set:
        """生成字符串的相邻二元组（bigram），用于模糊匹配粗筛"""
        s = s.lower()
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}

    def _add_role(self, category, role, role_dir, files):
        """登记一个角色及其音频，同时构建关键词映射"""
        full_paths = [os.path.join(role_dir, f) for f in files]
        self.categories.setdefault(category, {})[role] = full_paths
        if category not in self.category_order:
            self.category_order.append(category)
        for p in full_paths:
            self.file_to_role[p] = (category, role)
            name_without_ext = os.path.splitext(os.path.basename(p))[0]
            match = re.match(r'^(\d+)[_.\-\s]+(.+)$', name_without_ext)
            keyword = match.group(2) if match else name_without_ext
            # 过滤过短关键词，避免「杀」「死」等单字词误触发
            if keyword and len(keyword) >= self.min_keyword_len:
                self.keyword_map.append((keyword, p))

    def scan(self):
        """扫描音频目录，构建索引（支持多语音库）"""
        self.categories = {}
        self.category_order = []
        self.file_to_role = {}
        self.keyword_map = []
        self._bigram_index = {}
        self.role_map = {}
        self.role_category = {}

        roots = self._discover_audio_roots()
        if not roots:
            logger.error(f"[通用语音] 未发现任何语音库，请检查音频目录: {self.base_dir}")
            return

        for root in roots:
            self._scan_root(root)

        # 展平为兼容索引（重名时保留先扫描到的库，保证 sgs_voices 优先）
        for cat, roles in self.categories.items():
            for role, paths in roles.items():
                if role in self.role_map:
                    logger.warning(
                        f"[通用语音] 角色名「{role}」在多个语音库中重复，"
                        f"纯角色名匹配将使用库「{self.role_category[role]}」，"
                        f"可用「库名+角色名」精确点播（如「{cat}{role}」）"
                    )
                    continue
                self.role_map[role] = paths
                self.role_category[role] = cat

        self._sorted_roles = sorted(self.role_map.keys(), key=len, reverse=True)
        self._all_files_cache = [
            (f, role) for role, files in self.role_map.items() for f in files
        ]

        # 构建 bigram 索引（模糊匹配粗筛用）
        for idx, (kw, _) in enumerate(self.keyword_map):
            for bg in self._bigrams(kw):
                self._bigram_index.setdefault(bg, []).append(idx)

        # 计算音频库内容签名（列表图片缓存失效判断）
        sig_parts = []
        for paths in self.role_map.values():
            for p in paths:
                try:
                    st = os.stat(p)
                    sig_parts.append(f"{p}|{st.st_mtime_ns}|{st.st_size}")
                except OSError:
                    continue
        self.lib_signature = hashlib.md5('|'.join(sorted(sig_parts)).encode()).hexdigest()

        logger.info(
            f"[通用语音] 扫描完成: {len(self.category_order)} 个语音库"
            f"（{', '.join(self.category_order)}），{len(self.role_map)} 个角色，"
            f"{len(self.keyword_map)} 个关键词"
        )

    def _discover_audio_roots(self) -> list:
        """确定要扫描的语音库目录列表。

        组合规则（按扫描顺序，先扫描的库在角色重名时优先）：
        1. audio_root 指定的大目录（未配置则自动检测 voice/ 下的一级文件夹）；
        2. extra_libs 额外语音库目录列表，逐项追加（去重）；
        3. 全部为空时兜底 default_lib。
        """
        roots = []

        # 1. 用户配置的 audio_root（绝对路径或相对插件目录的大目录）
        custom = (self.audio_root or "").strip()
        if custom:
            p = custom if os.path.isabs(custom) else os.path.join(self.base_dir, custom)
            if os.path.isdir(p):
                roots.append(p)
            else:
                logger.warning(f"[通用语音] 配置的音频根目录不存在: {p}，回退为 voice/ 自动检测")

        # 2. 默认大目录 voice/：其下每个含角色目录的一级文件夹视为语音库
        if not roots:
            voice_root = os.path.join(self.base_dir, "voice")
            if os.path.isdir(voice_root):
                try:
                    entries = sorted(os.listdir(voice_root))
                except OSError:
                    entries = []
                for name in entries:
                    if name.startswith('.'):
                        continue
                    p = os.path.join(voice_root, name)
                    if os.path.isdir(p) and self._has_role_dirs(p):
                        roots.append(p)

        # 3. 额外语音库目录列表：每项可为单个库目录，或含多个库的大目录
        seen = {os.path.normcase(os.path.abspath(p)) for p in roots}
        for item in self.extra_libs:
            p = item if os.path.isabs(item) else os.path.join(self.base_dir, item)
            key = os.path.normcase(os.path.abspath(p))
            if key in seen:
                continue
            if os.path.isdir(p):
                roots.append(p)
                seen.add(key)
            else:
                logger.warning(f"[通用语音] 额外语音库目录不存在: {p}，已跳过")

        if roots:
            return roots

        # 4. 兜底：default_lib（默认 voice/sgs_voices）
        lib = (self.default_lib or "").strip()
        if lib:
            p = lib if os.path.isabs(lib) else os.path.join(self.base_dir, lib)
            if os.path.isdir(p) and self._has_role_dirs(p):
                return [p]

        return []

    def _has_role_dirs(self, dir_path: str) -> bool:
        """判断目录能否作为语音库：存在直接包含音频文件的子目录"""
        try:
            for name in os.listdir(dir_path):
                p = os.path.join(dir_path, name)
                if os.path.isdir(p) and any(self._is_audio(f) for f in os.listdir(p)):
                    return True
        except OSError:
            pass
        return False

    def _scan_root(self, root: str):
        """扫描一个音频根目录（大目录），支持两种布局：
        1. 平铺: 根/角色/音频        -> 角色归入默认库（根目录名）【根目录本身就是一个语音库时】
        2. 库式: 根/语音库/角色/音频 -> 语音库名 = 库文件夹名【voice/ 大目录或 extra_libs 指向含多库的大目录时】
        """
        root_name = os.path.basename(root)
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            return
        for name in entries:
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            try:
                files = self._sort_files([f for f in os.listdir(p) if self._is_audio(f)])
            except OSError:
                continue
            if files:
                # 布局1: p 直接是角色目录
                self._add_role(root_name, name, p, files)
                continue
            # 布局2: p 是语音库，其子目录是角色
            try:
                subs = sorted(os.listdir(p))
            except OSError:
                continue
            for sub in subs:
                sp = os.path.join(p, sub)
                if not os.path.isdir(sp):
                    continue
                try:
                    sub_files = self._sort_files([f for f in os.listdir(sp) if self._is_audio(f)])
                except OSError:
                    continue
                if sub_files:
                    self._add_role(name, sub, sp, sub_files)

    def match_role(self, message: str):
        """
        匹配「库名+角色名+序号」或「角色名+序号」模式
        返回: (audio_path_or_list, is_random, is_all) 或 None
        """
        lowered = message.lower()

        # 1. 库名+角色名：多语音库角色重名时可精确定位（如「三国杀曹操3」）
        for category in self.category_order:
            if not lowered.startswith(category.lower()):
                continue
            rest = message[len(category):].strip()
            if not rest:
                continue
            roles = self.categories.get(category, {})
            for role in sorted(roles, key=len, reverse=True):
                suffix = self._exact_suffix(rest, role)
                if suffix is not None:
                    result = self._resolve_suffix(category, role, suffix)
                    if result:
                        return result

        # 2. 纯角色名匹配（重名时取先扫描到的语音库）
        for role in self._sorted_roles:
            suffix = self._exact_suffix(message, role)
            if suffix is None:
                continue
            category = self.role_category.get(role, "")
            result = self._resolve_suffix(category, role, suffix)
            if result:
                return result

        return None

    @staticmethod
    def _exact_suffix(text: str, name: str):
        """
        文件夹名完全匹配：text 必须以 name 开头，且剩余部分只能是空或纯数字序号。
        返回后缀（"" = 随机、数字 = 序号）或 None（不完全匹配）。
        用于避免「曹操攻略」「袁绍说」之类以角色名开头但不是点播的消息误触发。
        """
        if not text.lower().startswith(name.lower()):
            return None
        suffix = text[len(name):].strip()
        if not suffix or suffix.isdigit():
            return suffix
        return None

    def _resolve_suffix(self, category: str, role: str, suffix: str):
        """解析角色名后的后缀: 空=随机, 0=全部, 数字=指定序号"""
        files = (self.categories.get(category, {}) or {}).get(role, [])
        if not files:
            files = self.role_map.get(role, [])
        if not suffix:
            return self._get_random_audio(role, files), True, False
        if suffix == "0":
            return self._get_all_audio(files), False, True
        if suffix.isdigit():
            return self._get_indexed_audio(role, files, int(suffix)), False, False
        return None

    def role_display(self, role: str) -> str:
        """角色显示名：多语音库时带库名前缀，避免混淆"""
        if len(self.category_order) <= 1:
            return role
        cat = self.role_category.get(role, "")
        return f"{cat}·{role}" if cat else role

    def role_of(self, path: str) -> str:
        """根据音频路径反查角色名"""
        info = self.file_to_role.get(path)
        return info[1] if info else os.path.basename(os.path.dirname(path))

    def display_of(self, path: str) -> str:
        """根据音频路径反查角色显示名（多库时含库名前缀，重名角色显示实际命中的库）"""
        info = self.file_to_role.get(path)
        if info:
            cat, role = info
            return f"{cat}·{role}" if len(self.category_order) > 1 else role
        return self.role_display(self.role_of(path))

    def _get_random_audio(self, role: str, files: list):
        """获取随机音频，避免连续重复"""
        if not files:
            return None

        if len(files) == 1:
            return files[0]

        last = self.last_played.get(role)
        # 尝试随机选择一个与上次不同的
        candidates = [f for f in files if f != last]
        if not candidates: # 理论上不会发生，除非只有1个文件且上面已处理
            candidates = files

        selected = random.choice(candidates)
        self.last_played[role] = selected
        return selected

    def _get_indexed_audio(self, role: str, files: list, index: int):
        """获取指定序号的音频"""
        if not files:
            return None

        # 序号从1开始
        real_index = index - 1

        if real_index < 0: # 序号0或负数，默认第一个
            return files[0]

        if real_index >= len(files):
            return files[-1] # 超出范围，返回最后一个

        return files[real_index]

    def _get_all_audio(self, files: list):
        """获取角色的所有音频文件列表"""
        return files

    def get_random_voices(self, count: int):
        """从所有角色中随机选取指定数量的语音，返回 [(path, role_name), ...]"""
        if not self._all_files_cache:
            return []
        count = min(count, len(self._all_files_cache))
        return random.sample(self._all_files_cache, count)

    def match_keyword(self, message: str, fuzzy_threshold=0.6):
        """
        匹配关键词（支持模糊匹配）
        返回: (audio_path, keyword) 或 None

        双向包含（kw in message / message in kw）合并择优：
        按重合文本长度降序，重合相同时按 完全相等 > 消息包含关键词 > 关键词包含消息 优先，
        避免短关键词（如「哈基米」）抢先于更长更具体的命中（如「哈基米的约定」）。
        """
        message = message.lower()

        # 1+2. 双向包含，择优而非随机
        hits = []  # (overlap_len, priority, path, keyword)
        for kw, p in self.keyword_map:
            k = kw.lower()
            if k in message:
                # 关键词包含于消息：重合长度 = 关键词长度；完全相等优先级最高
                hits.append((len(k), 0 if k == message else 1, p, kw))
            elif message in k:
                # 消息包含于关键词：重合长度 = 消息长度
                hits.append((len(message), 2, p, kw))
        if hits:
            best_overlap = max(h[0] for h in hits)
            best_priority = min(h[1] for h in hits if h[0] == best_overlap)
            candidates = [
                (p, kw) for ov, pr, p, kw in hits
                if ov == best_overlap and pr == best_priority
            ]
            return random.choice(candidates)

        # 3. 模糊匹配：bigram 粗筛 + quick_ratio 预过滤 + SequenceMatcher 精算
        #    仅当消息长度适中时尝试，避免对极短或极长消息进行昂贵计算
        if 2 <= len(message) <= 20:
            candidates = self._fuzzy_candidates(message, fuzzy_threshold)
            best_ratio = 0
            best_match = None
            for kw, path in candidates:
                ratio = difflib.SequenceMatcher(None, message, kw.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = (path, kw)
            if best_ratio >= fuzzy_threshold and best_match:
                return best_match

        return None

    def _fuzzy_candidates(self, message: str, fuzzy_threshold: float) -> list:
        """
        模糊匹配候选筛选：
        1) bigram 索引取与消息共享相邻字对的候选（通常只剩几十条）
        2) 无 bigram 命中时退化为全量，但用 quick_ratio 快速预过滤（比 ratio 快数倍）
        """
        idxs = set()
        for bg in self._bigrams(message):
            idxs.update(self._bigram_index.get(bg, ()))
        pool = [self.keyword_map[i] for i in idxs] if idxs else self.keyword_map

        # quick_ratio 粗筛：只保留可能达到阈值的候选，再交给上层精算
        return [
            (kw, path) for kw, path in pool
            if difflib.SequenceMatcher(None, message, kw.lower()).quick_ratio()
            >= fuzzy_threshold - 0.15
        ]

@register("astrbot_plugin_sgsvoice", "落日七号、复读机长", "通用语音玩梗插件 - 按语音库/角色名/台词关键词自动发送对应语音，支持多语音库与外部库目录（mp3/wav/m4a）", "1.6.1", "https://github.com/kvrry/astrbot_plugin_xgs_voice")
class SgsVoiceMeme(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self.base_dir = os.path.dirname(__file__)
        # 语音库大目录：默认 voice/（其下每个一级文件夹是一个语音库，sgs_voices 为默认库），
        # 可通过配置 audio_root 指定其它大目录；extra_lib_dirs 为额外语音库目录列表（任意位置，
        # 每项可为单个库目录或含多库的大目录），在自动识别之外追加
        self.audio_root = self.config.get("audio_root", "voice")
        self.default_lib = self.config.get("default_lib", "voice/sgs_voices")
        self.extra_libs = [
            str(x).strip() for x in (self.config.get("extra_lib_dirs", []) or [])
            if str(x).strip()
        ]

        # 加载配置（min_keyword_len 等需在 VoiceManager 前读取）
        self._load_config()

        # 初始化语音管理器
        self.voice_manager = VoiceManager(
            self.base_dir, self.audio_root, self.default_lib,
            self.min_keyword_len, self.extra_libs
        )

        # 统计信息
        self.trigger_count = 0

        # 加载持久化数据目录
        self.data_dir = StarTools.get_data_dir("sgsvoice")

        # 角色列表图片缓存路径 + 内容签名
        self._role_list_img_path = os.path.join(self.data_dir, "role_list_cache.png")
        self._role_list_signature = ""  # 缓存时的音频库签名

        # 清理过期音频缓存
        self._cleanup_cache()

        logger.info(f"[通用语音] 插件初始化完成！")
        logger.info(f"[通用语音] 语音库: {self.voice_manager.category_order}")
        logger.info(f"[通用语音] 数据目录: {self.data_dir}")

    def _wav_cache_path(self, audio_path: str) -> str:
        """计算音频在 cache_wav 下的缓存路径。

        插件目录内的音频镜像源相对目录结构；目录外的音频（extra_libs 等）
        用源目录哈希隔离，避免 ".." 相对路径逃逸缓存目录或不同库间重名冲突。
        """
        cache_dir = os.path.join(self.data_dir, "cache_wav")
        try:
            rel_path = os.path.relpath(audio_path, self.base_dir)
        except ValueError:
            rel_path = None  # 跨盘符等情况无法取相对路径
        if not rel_path or rel_path == ".." or rel_path.startswith(".." + os.sep):
            digest = hashlib.md5(
                os.path.dirname(audio_path).encode("utf-8", "ignore")
            ).hexdigest()[:12]
            rel_path = os.path.join("_external", digest, os.path.basename(audio_path))
        return os.path.join(cache_dir, os.path.splitext(rel_path)[0] + ".wav")

    def _get_wav_path(self, audio_path: str) -> str:
        """
        获取音频的 WAV 版本路径。如果是 MP3，则转换为 WAV。
        用于兼容某些只支持 WAV 的平台（如 qq_official）。
        """
        if audio_path.lower().endswith(".wav"):
            return audio_path

        wav_path = self._wav_cache_path(audio_path)

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
            logger.error(f"[通用语音] 音频转换失败 (MP3 -> WAV): {e}")
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
        cache_dir = os.path.join(self.data_dir, "cache_merged")
        os.makedirs(cache_dir, exist_ok=True)

        # 用路径列表的哈希值做缓存文件名
        key = hashlib.md5('|'.join(sorted(audio_paths)).encode()).hexdigest()
        merged_path = os.path.join(cache_dir, f"{key}.mp3")

        if os.path.exists(merged_path):
            return merged_path

        # 构建 ffmpeg concat filter：[0:a][silence][1:a][silence]...concat=n:out_type=a
        # 所有输入先统一为 44100Hz / s16 / mono，避免采样率或声道不一致导致 concat 报错
        inputs = []
        filter_parts = []
        concat_inputs = []

        for i, path in enumerate(audio_paths):
            inputs.extend(["-i", path])
            if i > 0:
                # 在前一个片段后插入 0.6s 静音（参数与统一后的输入一致）
                silence_label = f"[s{i}]"
                filter_parts.append(
                    f"anullsrc=r=44100:cl=mono:d=0.6,"
                    f"aformat=sample_fmts=s16:channel_layouts=mono{silence_label}"
                )
                concat_inputs.append(silence_label)
            audio_label = f"[a{i}]"
            filter_parts.append(
                f"[{i}:a]aresample=44100,aformat=sample_fmts=s16:channel_layouts=mono{audio_label}"
            )
            concat_inputs.append(audio_label)

        concat_n = len(concat_inputs)
        filter_parts.append(
            f"{''.join(concat_inputs)}concat=n={concat_n}:v=0:a=1[out]"
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
            logger.error(f"[通用语音] 音频合并失败: {e}")
            return None

    def _load_config(self):
        """从配置对象加载设置"""
        self.need_llm_patterns = self.config.get("llm_wake_patterns", DEFAULT_LLM_PATTERNS)
        self.wake_word_prefix = self.config.get("wake_word_prefix", "/")
        self.require_prefix = self.config.get("require_prefix", False)
        self.enable_group_only = self.config.get("enable_group_only", True)
        self.private_chat_llm_mode = self.config.get("private_chat_llm_mode", "smart")
        self.fuzzy_threshold = self.config.get("fuzzy_threshold", 0.6)
        self.min_keyword_len = max(1, int(self.config.get("min_keyword_len", 2) or 2))
        self.cache_max_days = max(0, int(self.config.get("cache_max_days", 30) or 30))

    def _cleanup_cache(self):
        """清理缓存：删除源文件已不存在的 WAV 缓存（孤儿缓存），以及超期的合并缓存"""
        try:
            # 1. cache_wav: 以当前扫描索引重建有效缓存集合，不在集合内的视为孤儿缓存删除。
            #    统一走 _wav_cache_path 计算，插件目录内/外的音频均可正确反查
            cache_wav = os.path.join(self.data_dir, "cache_wav")
            if os.path.isdir(cache_wav):
                valid_paths = {
                    self._wav_cache_path(p) for p in self.voice_manager.file_to_role
                }
                removed = 0
                for root, dirs, files in os.walk(cache_wav):
                    for f in files:
                        p = os.path.join(root, f)
                        if p in valid_paths:
                            continue
                        try:
                            os.remove(p)
                            removed += 1
                        except OSError:
                            continue
                if removed:
                    logger.info(f"[通用语音] 缓存清理: 删除 {removed} 个无效 WAV 缓存")

            # 2. cache_merged: 按保留天数清理
            if self.cache_max_days > 0:
                cache_merged = os.path.join(self.data_dir, "cache_merged")
                if os.path.isdir(cache_merged):
                    cutoff = time.time() - self.cache_max_days * 86400
                    removed = 0
                    for f in os.listdir(cache_merged):
                        p = os.path.join(cache_merged, f)
                        try:
                            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                                os.remove(p)
                                removed += 1
                        except OSError:
                            continue
                    if removed:
                        logger.info(f"[通用语音] 缓存清理: 删除 {removed} 个超期合并缓存")
        except Exception as e:
            logger.error(f"[通用语音] 缓存清理失败: {e}")

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

    def _generate_role_list_image(self, category=None):
        """生成角色列表精美图片，带内容签名缓存；category 非空时只列该语音库"""
        if category:
            roles_map = self.voice_manager.categories.get(category, {})
            role_names = list(roles_map.keys())
        else:
            role_names = list(self.voice_manager.role_map.keys())
        role_count = len(role_names)

        # 缓存签名 = 音频库内容签名 + 目标库（库内改名/增删音频都会触发重建）
        cache_key = f"{self.voice_manager.lib_signature}|cat:{category or '*'}"

        if (os.path.exists(self._role_list_img_path)
                and self._role_list_signature == cache_key
                and role_count > 0):
            return self._role_list_img_path

        try:
            from PIL import Image, ImageDraw, ImageFont
        except ImportError:
            logger.error("[通用语音] 生成角色列表图片需要 Pillow 库，请安装: pip install Pillow")
            return None

        # 按拼音首字母排序
        try:
            from pypinyin import lazy_pinyin
            roles = sorted(role_names, key=lambda h: lazy_pinyin(h))
        except ImportError:
            import locale
            try:
                roles = sorted(role_names, key=locale.strxfrm)
            except Exception:
                roles = sorted(role_names)
        if not roles:
            return None

        # ---- 布局参数 ----
        cols = 8
        rows_count = -(-len(roles) // cols)  # 向上取整

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
        if category:
            title = f"{category} · 角色列表"
        else:
            title = "语音库 · 角色列表"
        draw.text((img_w // 2, 35), title, fill='#FFFFFF', font=font_title, anchor='mt')

        # 装饰线
        line_w = 200
        lx = (img_w - line_w) // 2
        draw.line([(lx, 72), (lx + line_w, 72)], fill='#3A3A42', width=2)
        # 装饰线两端点缀
        draw.ellipse([(lx - 3, 69), (lx + 3, 75)], fill='#E62828')
        draw.ellipse([(lx + line_w - 3, 69), (lx + line_w + 3, 75)], fill='#E62828')

        if category:
            subtitle = f"共收录 {role_count} 位角色"
        else:
            cat_count = len(self.voice_manager.category_order)
            subtitle = f"共收录 {role_count} 位角色" + (f" · {cat_count} 个语音库" if cat_count > 1 else "")
        draw.text((img_w // 2, 85), subtitle, fill='#9999A2', font=font_sub, anchor='mt')

        # ---- 绘制角色卡片网格 ----
        for i, role in enumerate(roles):
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

            # 角色名（居中，超长名称自动截断；指定库时不带库前缀，汇总时带）
            if category:
                voice_count = len(self.voice_manager.categories[category][role])
                display_name = role
            else:
                voice_count = len(self.voice_manager.role_map[role])
                display_name = self.voice_manager.role_display(role)
            bbox = draw.textbbox((0, 0), display_name, font=font_name)
            text_w = bbox[2] - bbox[0]
            truncated = False
            while text_w > card_w - 12 and len(display_name) > 1:
                display_name = display_name[:-1]
                truncated = True
                bbox = draw.textbbox((0, 0), display_name + '…', font=font_name)
                text_w = bbox[2] - bbox[0]
            if truncated:
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
        footer = "发送「角色名0」听全部语音  ·  发送「随机台词N」随机N条"
        draw.text((img_w // 2, img_h - 20), footer, fill='#555560', font=font_count, anchor='mm')

        # ---- 保存 ----
        os.makedirs(os.path.dirname(self._role_list_img_path), exist_ok=True)
        img.save(self._role_list_img_path, quality=95)
        self._role_list_signature = cache_key
        logger.info(f"[通用语音] 角色列表图片已生成: {self._role_list_img_path}")
        return self._role_list_img_path

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

        voice_infos = []  # [(role_name, voice_text, path), ...]
        trigger_keyword = ""

        # ---- 功能: 随机台词+数字 ----
        random_match = re.match(r'^随机台词\s*(\d+)$', message)
        if random_match:
            count = min(int(random_match.group(1)), 5)
            results = self.voice_manager.get_random_voices(count)
            if results:
                for path, role in results:
                    text = self._extract_voice_text(path)
                    display_name = self.voice_manager.role_display(role)
                    voice_infos.append((display_name, text, path))
                trigger_keyword = f"随机台词 {count}条"

        # ---- 功能: 角色列表（可指定语音库，如「角色列表 鬼畜语录」）----
        # 兼容旧触发词「英雄列表」
        elif (message in ("角色列表", "英雄列表")
                or message.startswith("角色列表 ") or message.startswith("英雄列表 ")):
            head, _, tail = message.partition(" ")
            lib = tail.strip() if tail else ""
            if lib and lib not in self.voice_manager.category_order:
                yield event.plain_result(f"❌ 未找到语音库「{lib}」，可用「/v list」查看全部语音库")
                event.stop_event()
                return
            img_path = self._generate_role_list_image(lib or None)
            if img_path:
                logger.info(f"[通用语音] 发送角色列表图片 (库: {lib or '全部'})")
                self.trigger_count += 1
                try:
                    yield event.chain_result([
                        Comp.Image(file=img_path)
                    ])
                except Exception as e:
                    logger.error(f"[通用语音] 发送角色列表图片失败: {e}")
                    yield event.plain_result(f"角色列表图片发送失败，请检查日志。")
                event.stop_event()
                return

        # ---- 原有: 角色名+序号 ----
        else:
            role_match = self.voice_manager.match_role(message)
            if role_match:
                result, is_random, is_all = role_match
                if is_all:
                    # 角色名+0：全部语音
                    # 从路径中获取实际角色名（匹配后的标准名）
                    role_name = self.voice_manager.role_of(result[0]) if result else message.rstrip('0').strip()
                    display_name = self.voice_manager.role_display(role_name)
                    for path in result:
                        text = self._extract_voice_text(path)
                        voice_infos.append((display_name, text, path))
                    trigger_keyword = f"{display_name} 全部语音（共{len(result)}条）"
                else:
                    # 角色名+序号：单条语音
                    path = result
                    display_name = self.voice_manager.display_of(path)
                    text = self._extract_voice_text(path)
                    voice_infos.append((display_name, text, path))
                    trigger_keyword = f"{display_name}: {text}"
            else:
                # ---- 原有: 关键词模糊匹配 ----
                keyword_match = self.voice_manager.match_keyword(message, self.fuzzy_threshold)
                if keyword_match:
                    path, kw = keyword_match
                    display_name = self.voice_manager.display_of(path)
                    voice_infos.append((display_name, kw, path))
                    trigger_keyword = kw

        if not voice_infos:
            return

        # 过滤无效路径
        voice_infos = [(h, t, p) for h, t, p in voice_infos if p and os.path.exists(p)]
        if not voice_infos:
            return

        n = len(voice_infos)
        logger.info(f"[通用语音] 触发: '{trigger_keyword}', 发送 {n} 条语音")
        self.trigger_count += 1

        # ---- 多条语音：提示所有台词 + 5条限制 + 音频合并 ----
        if n > 1:
            need_merge = n > 4
            individual = voice_infos[:3] if need_merge else voice_infos
            merged = voice_infos[3:] if need_merge else []

            # 构建提示文本：列出所有语音的角色名+台词
            lines = [f"🎭 {trigger_keyword}"]
            for i, (role, text, _) in enumerate(voice_infos, 1):
                lines.append(f"{i}. 【{role}】{text}")
            if need_merge:
                lines.append(f"（第4~{n}条已合并为一条音频）")

            try:
                yield event.plain_result('\n'.join(lines))
                await asyncio.sleep(0.3)
            except Exception:
                pass

            # 逐条发送单独语音（前3条）
            for role, text, path in individual:
                final_audio_path = self._get_wav_path(path)
                try:
                    yield event.chain_result([
                        Comp.Record(file=final_audio_path, url=final_audio_path)
                    ])
                    await asyncio.sleep(0.6)
                except Exception as e:
                    logger.error(f"[通用语音] 发送语音失败: {e}")

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
                        logger.error(f"[通用语音] 发送合并语音失败: {e}")
                else:
                    # 合并失败，逐条发送（可能超出5条限制）
                    for role, text, path in merged:
                        final_audio_path = self._get_wav_path(path)
                        try:
                            yield event.chain_result([
                                Comp.Record(file=final_audio_path, url=final_audio_path)
                            ])
                            await asyncio.sleep(0.6)
                        except Exception as e:
                            logger.error(f"[通用语音] 发送语音失败: {e}")
        else:
            # 单条语音直接发送
            _, _, path = voice_infos[0]
            final_audio_path = self._get_wav_path(path)
            try:
                yield event.chain_result([
                    Comp.Record(file=final_audio_path, url=final_audio_path)
                ])
            except Exception as e:
                logger.error(f"[通用语音] 发送语音失败: {e}")

        # 智能判断是否需要 LLM 回复
        if self._needs_llm_response(message, event):
            yield event.request_llm(prompt=message)
        else:
            event.stop_event()

    @filter.command_group("v")
    def v_group(self):
        pass

    @v_group.command("help")
    async def v_help(self, event: AstrMessageEvent):
        prefix_mode = f"前缀触发（{self.wake_word_prefix}）" if self.require_prefix else "自由触发"
        help_text = f"""🎭 通用语音插件 v1.6.1

📌 功能：
1. 「角色名+序号」点播语音（如：SP关羽3）
2. 「角色名+0」发送该角色全部语音（如：曹操0）
3. 角色名不带序号随机播放（不重复）
4. 「随机台词N」随机发送N条语音（上限5条，如：随机台词3）
5. 群聊发送「角色列表」查看全部角色图片（「角色列表 库名」只看某个库；兼容旧触发词「英雄列表」）
6. 关键词匹配（含模糊匹配，阈值: {self.fuzzy_threshold*100:.0f}%）
7. 多语音库：voice/ 下自动识别 + 配置额外库目录（extra_lib_dirs），
   角色重名时可用「库名+角色名」精确点播（如：三国杀曹操3）

当前状态：
• 触发模式: {prefix_mode}
• 语音库: {len(self.voice_manager.category_order)} 个（{', '.join(self.voice_manager.category_order)}）
• 角色数量: {len(self.voice_manager.role_map)}
• 关键词数: {len(self.voice_manager.keyword_map)}

可用指令：
• /v help - 帮助
• /v list - 列出所有语音库
• /v list <库名> - 指定语音库的角色列表图片
• /v list <库名> <角色名> - 列出该角色文件夹下的音频文件（也可只输角色名全库查找）
• /v stats - 统计
• /v reload - 重载配置和音频
"""
        yield event.plain_result(help_text)

    @v_group.command("list")
    async def v_list(self, event: AstrMessageEvent, lib: str = "", role: str = ""):
        """
        三级查询：
        /v list                  - 列出所有语音库
        /v list <库名>            - 该语音库的角色列表图片
        /v list <库名> <角色名>   - 列出该角色文件夹下的音频文件（台词）
        /v list <角色名>          - 角色名不在库名位置时，全库查找该角色并列出音频文件
        """
        lib = (lib or "").strip()
        role = (role or "").strip()

        # 1. 无参数：语音库列表
        if not lib:
            lines = ["🎭 语音库列表"]
            for cat in self.voice_manager.category_order:
                role_count = len(self.voice_manager.categories.get(cat, {}))
                lines.append(f"• {cat}（{role_count} 位角色）")
            lines.append("\n发送「角色列表」查看全部角色；「/v list <库名>」看库角色；「/v list <库名> <角色名>」看角色音频文件")
            yield event.plain_result('\n'.join(lines))
            return

        # 2. 第一个参数是库名
        if lib in self.voice_manager.category_order:
            if role:
                # 2a. 库名+角色名：列出该角色文件夹下的音频文件
                files = (self.voice_manager.categories.get(lib, {}) or {}).get(role)
                if not files:
                    yield event.plain_result(f"❌ 语音库「{lib}」中没有角色「{role}」，可用「/v list {lib}」查看库内角色")
                    return
                lines = [f"🎭 {lib}·{role}（{len(files)} 条音频）"]
                for i, p in enumerate(files, 1):
                    lines.append(f"{i}. {self._extract_voice_text(p)}")
                yield event.plain_result('\n'.join(lines))
                return
            # 2b. 仅库名：角色列表图片（现状）
            img_path = self._generate_role_list_image(lib)
            if img_path:
                try:
                    yield event.chain_result([
                        Comp.Image(file=img_path)
                    ])
                except Exception as e:
                    yield event.plain_result(f"❌ 图片发送失败: {e}")
            else:
                yield event.plain_result("❌ 角色列表图片生成失败，请检查日志或安装 Pillow 库。")
            return

        # 3. 第一个参数不是库名：当作角色名，全库查找
        files = self.voice_manager.role_map.get(lib)
        if files:
            cat = self.voice_manager.role_category.get(lib, "")
            prefix = f"{cat}·{lib}" if cat else lib
            lines = [f"🎭 {prefix}（{len(files)} 条音频）"]
            for i, p in enumerate(files, 1):
                lines.append(f"{i}. {self._extract_voice_text(p)}")
            yield event.plain_result('\n'.join(lines))
            return

        # 4. 都没有命中
        yield event.plain_result(f"❌ 未找到语音库「{lib}」或角色「{lib}」，可用「/v list」查看全部语音库")

    @v_group.command("stats")
    async def v_stats(self, event: AstrMessageEvent):
        role_count = len(self.voice_manager.role_map)
        keyword_count = len(self.voice_manager.keyword_map)

        stats_text = f"""📊 统计信息
本次触发: {self.trigger_count} 次
语音库: {len(self.voice_manager.category_order)} 个
收录角色: {role_count} 位
收录关键词: {keyword_count} 条
"""
        yield event.plain_result(stats_text)

    @v_group.command("reload")
    async def v_reload(self, event: AstrMessageEvent):
        """重新加载配置和音频文件"""
        # 1. 重新加载配置
        self._load_config()
        # 2. 按新配置重建语音管理器（min_keyword_len、extra_lib_dirs 等可能变化）
        self.extra_libs = [
            str(x).strip() for x in (self.config.get("extra_lib_dirs", []) or [])
            if str(x).strip()
        ]
        self.audio_root = self.config.get("audio_root", "voice")
        self.default_lib = self.config.get("default_lib", "voice/sgs_voices")
        self.voice_manager = VoiceManager(
            self.base_dir, self.audio_root, self.default_lib,
            self.min_keyword_len, self.extra_libs
        )
        # 3. 清理过期缓存
        self._cleanup_cache()
        # 4. 清除角色列表图片缓存
        self._role_list_signature = ""

        yield event.plain_result(
            f"✅ 已重载配置并扫描音频目录！\n"
            f"语音库: {len(self.voice_manager.category_order)} 个\n"
            f"角色: {len(self.voice_manager.role_map)}\n"
            f"关键词: {len(self.voice_manager.keyword_map)}\n"
            f"触发模式: {'前缀触发' if self.require_prefix else '自由触发'}\n"
            f"模糊匹配阈值: {self.fuzzy_threshold*100:.0f}%\n"
            f"关键词最短长度: {self.min_keyword_len}"
        )
