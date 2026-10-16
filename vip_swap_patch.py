#!/usr/bin/env python3
"""fnmusic-ext: 网易 VIP 试听曲目自动换源到洛雪同曲完整版（幂等补丁 v3.2）。

v3 相比 v2：
  (a) 播放期换源命中后，额外注册一次后台整轨下载 —— 换源出流走 307 跳转，
      代理不经手字节，tee 落盘不会触发；由后台下载把完整版存进本地曲库。
  (b) 后台整轨下载（红心收藏自动绑定 / 边听边存兜底）内部也走洛雪换源 ——
      否则会把网易 20~35 秒试听片段当整曲落库，污染曲库。

v3.2 修复识别失效：
  (a) VIP 判定改为 needs_vip 字段优先（上游用它在标题前动态加 [VIP] 前缀），
      兼容标题已带前缀的情况，统一经 strip_source_tag 归一。
  (b) 网易账号歌单曲目不进搜索缓存、内存元数据冷启动为空 —— 增加磁盘歌单
      缓存（nm_playlists_cache 等）反查兜底。
  (c) 匹配两轮：标题归一后相等 -> 放宽为互相包含（限长度差），均要求歌手交集。

用法: python3 vip_swap_patch.py [--apply|--revert|--check]

安装位置不是默认路径时，用环境变量覆盖：
  FNMUSIC_APP_PY=/your/path/proxy/app.py python3 vip_swap_patch.py --apply
"""
import hashlib
import os
import shutil
import sys
import time

APP = os.environ.get("FNMUSIC_APP_PY") or "/vol1/@appcenter/fnmusic-ext/repo/proxy/app.py"

MARK_BEGIN = "    # === FNMUSIC_VIP_SWAP begin ==="
MARK_END = "    # === FNMUSIC_VIP_SWAP end ==="
FETCH_BEGIN = "        # === FNMUSIC_VIP_SWAP fetch begin ==="
FETCH_END = "        # === FNMUSIC_VIP_SWAP fetch end ==="
HELP_BEGIN = "# === FNMUSIC_VIP_SWAP: 网易 VIP 试听曲目 -> 洛雪同曲完整版 ==="
HELP_END = "# === FNMUSIC_VIP_SWAP: end ==="

# ---- 1) CONF 默认值 -------------------------------------------------------
CONF_OLD = '    "fav_auto_bind": os.environ.get("FNMUSIC_FAV_AUTO_BIND", "false").lower() in ("true", "1", "yes"),\n'
CONF_NEW = CONF_OLD + (
    '    # 网易 VIP 试听曲目播放期自动换源到洛雪（见 stream_track 处说明）；关=false\n'
    '    "vip_swap": os.environ.get("FNMUSIC_VIP_SWAP", "true").lower() in ("true", "1", "yes"),\n'
)

# ---- 2) .env 热重载白名单 -------------------------------------------------
WATCH_OLD = '    "FNMUSIC_FAV_AUTO_BIND": ("fav_auto_bind", "bool"),\n'
WATCH_NEW = WATCH_OLD + '    "FNMUSIC_VIP_SWAP": ("vip_swap", "bool"),\n'

# ---- 3) 辅助函数块 -------------------------------------------------------
ANCHOR_HELPERS = '@app.api_route("/music/api/v1/track/stream", methods=["GET", "HEAD"])'

HELPERS = r'''# === FNMUSIC_VIP_SWAP: 网易 VIP 试听曲目 -> 洛雪同曲完整版 ===
# 背景：无 VIP 权益的网易账号点 VIP 曲目时，musicbox 只拿到 30~60 秒试听片段
# （上游用 needs_vip 字段在歌名前动态加 [VIP] 前缀提示）。本补丁在播放取流
# 阶段识别这类曲目，用 "歌名 歌手" 去洛雪搜同曲完整版，命中则把洛雪条目插到
# 候选最前，复用既有跨源候选 + 307 跳转机制出流；命中不了原样回落网易试听，
# 行为不劣化。后台整轨下载（红心自动绑定 / 边听边存兜底）走同一判定，保证
# 落库的是完整版而不是试听片段。开关：.env 的 FNMUSIC_VIP_SWAP（热重载）。
_VIP_SWAP_CACHE: "dict[str, tuple[str | None, float]]" = {}
_VIP_SWAP_TTL_S = 6 * 3600.0
_VIP_SWAP_MISS_TTL_S = 900.0
_VIP_SWAP_CACHE_MAX = 512
_VIP_SWAP_TAG = "[VIP] "


def _vip_swap_enabled() -> bool:
    return bool(CONF.get("vip_swap", True))


def _vip_probe_one(src: "dict | None") -> "tuple[str, str] | None":
    """单个条目判是否"仅试听"：needs_vip=True 或标题带 [VIP] 前缀。

    标题统一经 strip_source_tag 归一（可能叠加 [music box] 等来源标记）。
    """
    if not isinstance(src, dict):
        return None
    try:
        title, artist, _album = _tag_fields(src)
    except Exception:
        return None
    title = str(title or "")
    if src.get("needs_vip") is not True and not title.startswith(_VIP_SWAP_TAG):
        return None
    name = strip_source_tag(title).strip()
    if not name:
        return None
    return name, str(artist or "").strip()


def _vip_swap_probe(guid: str, item: "dict | None") -> "tuple[str, str] | None":
    """判定该网易曲目是否"仅试听"：搜索会话在前，内存在线元数据兜底。"""
    return _vip_probe_one(item) or _vip_probe_one(_online_meta_get(guid))


def _vip_norm(text: object) -> str:
    return re.sub(r"[\s\-_·・.,，。!！?？'\"“”‘’:：;；、()\[\]【】&+/~]+", "", str(text or "").lower())


def _vip_pick_match(items, title: str, artist: str) -> "dict | None":
    """从洛雪候选里挑同曲完整版。

    两轮：先标题归一后相等；再放宽为互相包含（限长度差，防"年轮"/"年轮说"误配）。
    两轮都要求歌手名有交集，拦掉翻唱与同名不同曲；时长 <60 秒视为片段弃用。
    """
    want = _vip_norm(title)
    if not want:
        return None
    tokens = [t for t in (_vip_norm(x) for x in re.split(r"[&/,、;；\s]+", str(artist or ""))) if t]

    def _ok(it: dict, strict: bool) -> bool:
        cand_title = _vip_norm(it.get("title") or it.get("name"))
        if not cand_title:
            return False
        if strict:
            if cand_title != want:
                return False
        else:
            if not (cand_title in want or want in cand_title):
                return False
            if abs(len(cand_title) - len(want)) > 6:
                return False
        cand_artist = _vip_norm(it.get("artist"))
        if tokens and not any(t in cand_artist for t in tokens):
            return False
        try:
            dur = float(it.get("duration_s") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if 0 < dur < 60:
            return False
        return True

    pool = [it for it in (items or []) if isinstance(it, dict)]
    for it in pool:
        if _ok(it, True):
            return it
    for it in pool:
        if _ok(it, False):
            return it
    return None


async def _vip_lx_swap_target(request: Request, guid: str, item: "dict | None") -> "str | None":
    """网易仅试听曲目 -> 洛雪同曲完整版 guid（命中/未命中都进 TTL 缓存）。"""
    now = time.monotonic()
    hit = _VIP_SWAP_CACHE.get(guid)
    if hit and now < hit[1]:
        return hit[0]
    target = None
    try:
        probe = _vip_swap_probe(guid, item)
        if not probe:
            # 网易账号歌单曲目不进搜索缓存，内存元数据冷启动后可能为空：
            # 落到磁盘歌单缓存反查（其中标题带 [VIP] 前缀）。
            try:
                disk = await asyncio.to_thread(_lookup_playlist_cache_track, guid)
            except Exception:
                disk = None
            probe = _vip_probe_one(disk)
        if probe and probe[0]:
            keyword = f"{probe[0]} {probe[1]}".strip()
            token = _FETCH_SCOPE.set(_credential_scope(request) + ":vipswap")
            try:
                items = await asyncio.wait_for(
                    fetch_lx_search(get_lx_client(request.app), keyword,
                                    CONF.get("lx_search_limit") or 20,
                                    CONF.get("lx_sources") or None),
                    timeout=4.0,
                )
            finally:
                _FETCH_SCOPE.reset(token)
            matched = _vip_pick_match(items, probe[0], probe[1])
            if matched:
                cand = online_guid_from_item(matched)
                if cand and cand != guid and _source_enabled(cand):
                    target = cand
                    logger.info("vip swap %s (%s - %s) -> %s",
                                guid, probe[0][:30], probe[1][:20], cand)
            if target is None:
                logger.info("vip swap miss %s (%s - %s) searched=%d",
                            guid, probe[0][:30], probe[1][:20], len(items or []))
    except Exception as e:
        logger.warning("vip swap failed for %s: %s", guid, type(e).__name__)
    _VIP_SWAP_CACHE[guid] = (target, now + (_VIP_SWAP_TTL_S if target else _VIP_SWAP_MISS_TTL_S))
    while len(_VIP_SWAP_CACHE) > _VIP_SWAP_CACHE_MAX:
        _VIP_SWAP_CACHE.pop(next(iter(_VIP_SWAP_CACHE)))
    return target
# === FNMUSIC_VIP_SWAP: end ===


'''

# ---- 4) 取流处调用 -------------------------------------------------------
CALL_OLD = """    item, entry = _retained_track(request, guid)
    candidates = [guid]
"""

CALL_NEW = """    item, entry = _retained_track(request, guid)
    candidates = [guid]
    # === FNMUSIC_VIP_SWAP begin ===
    # 网易 VIP 试听曲目：优先改走洛雪同曲完整版（命中则 307 跳到洛雪条目）
    if _vip_swap_enabled() and source_from_online_guid(guid) == "netease":
        try:
            _vip_alt = await _vip_lx_swap_target(request, guid, item)
        except Exception:
            _vip_alt = None
        if _vip_alt and _vip_alt != guid:
            candidates.insert(0, _vip_alt)
            # 换源出流走 307 跳转，代理不经手字节、tee 无法落盘；另起后台整轨
            # 下载把完整版存进本地曲库（同一开关做门禁，完成后自动去重）。
            _register_background_fetch(request, guid, "vip_swap")
    # === FNMUSIC_VIP_SWAP end ===
"""

# ---- 5) 后台整轨下载也换源 -----------------------------------------------
FETCH_OLD = """        fake_request = _synth_request(cred_headers)
        opened = await _open_online_stream(fake_request, guid, None, force_mp3=force_mp3,
                                           fresh_url=fresh_url or force_mp3)
"""

FETCH_NEW = """        fake_request = _synth_request(cred_headers)
        # === FNMUSIC_VIP_SWAP fetch begin ===
        # 网易 VIP 试听曲目：后台整轨下载同样改从洛雪取完整版，避免把 20~35 秒
        # 试听片段当整曲落库。落库仍用原网易 guid，保证曲库命名与官方绑定一致。
        _vip_fetch_guid = guid
        if _vip_swap_enabled() and source_from_online_guid(guid) == "netease":
            try:
                _vip_f_item, _ = _retained_track(fake_request, guid)
                _vip_f_alt = await _vip_lx_swap_target(fake_request, guid, _vip_f_item)
            except Exception:
                _vip_f_alt = None
            if _vip_f_alt and _vip_f_alt != guid:
                _vip_fetch_guid = _vip_f_alt
        opened = await _open_online_stream(fake_request, _vip_fetch_guid, None, force_mp3=force_mp3,
                                           fresh_url=fresh_url or force_mp3)
        # === FNMUSIC_VIP_SWAP fetch end ===
"""


def read():
    with open(APP, "r", encoding="utf-8") as fp:
        return fp.read()


def check(src):
    print("conf_key:", src.count(CONF_NEW))
    print("watch_key:", src.count(WATCH_NEW))
    print("helpers:", src.count(HELP_BEGIN))
    print("call_block:", src.count(MARK_BEGIN))
    print("fetch_block:", src.count(FETCH_BEGIN))
    print("anchor_helpers:", src.count(ANCHOR_HELPERS))
    print("anchor_call:", src.count(CALL_OLD))
    print("anchor_fetch:", src.count(FETCH_OLD))
    print("anchor_conf:", src.count(CONF_OLD))
    print("anchor_watch:", src.count(WATCH_OLD))


def _drop_block(out, begin, end, tail="\n"):
    """跨版本 revert 兜底：删掉 begin/end 之间整段（含 end 行）。"""
    if begin not in out:
        return out
    head = out.split(begin)[0]
    parts = out.split(end + tail, 1)
    if len(parts) < 2:
        return out
    return head + parts[1]


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--apply"
    src = read()

    if mode == "--check":
        check(src)
        return 0

    if mode == "--revert":
        if not any(x in src for x in (FETCH_NEW, CALL_NEW, HELPERS, HELP_BEGIN,
                                      CONF_NEW, WATCH_NEW, MARK_BEGIN, FETCH_BEGIN)):
            print("nothing to revert")
            return 0
        out = src
        # 优先精确回替换；文本已随补丁版本变化的，按标记切分兜底
        if FETCH_NEW in out:
            out = out.replace(FETCH_NEW, FETCH_OLD, 1)
        else:
            out = _drop_block(out, FETCH_BEGIN, FETCH_END)
        if CALL_NEW in out:
            out = out.replace(CALL_NEW, CALL_OLD, 1)
        else:
            out = _drop_block(out, MARK_BEGIN, MARK_END)
        if HELPERS in out:
            out = out.replace(HELPERS, "", 1)
        elif HELP_BEGIN in out:
            out = _drop_block(out, HELP_BEGIN, HELP_END, "\n\n\n")
        if CONF_NEW in out:
            out = out.replace(CONF_NEW, CONF_OLD, 1)
        if WATCH_NEW in out:
            out = out.replace(WATCH_NEW, WATCH_OLD, 1)
        leftover = [x[:60] for x in (FETCH_BEGIN, MARK_BEGIN, HELP_BEGIN, CONF_NEW, WATCH_NEW) if x in out]
        if leftover:
            print("REVERT INCOMPLETE, still present:", leftover)
            return 3
        if out == src:
            print("nothing to revert")
            return 0
        shutil.copy2(APP, APP + ".bak.revert." + time.strftime("%Y%m%d-%H%M%S"))
        with open(APP, "w", encoding="utf-8") as fp:
            fp.write(out)
        print("reverted")
        return 0

    if MARK_BEGIN in src or FETCH_BEGIN in src:
        print("already patched; nothing to do")
        return 0

    for name, txt, want in (("anchor_helpers", ANCHOR_HELPERS, 1), ("anchor_call", CALL_OLD, 1),
                            ("anchor_fetch", FETCH_OLD, 1), ("anchor_conf", CONF_OLD, 1),
                            ("anchor_watch", WATCH_OLD, 1)):
        got = src.count(txt)
        if got != want:
            print("ANCHOR MISMATCH %s: got %d want %d" % (name, got, want))
            return 2

    bak = APP + ".bak.vipswap." + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(APP, bak)

    out = src.replace(CONF_OLD, CONF_NEW, 1)
    out = out.replace(WATCH_OLD, WATCH_NEW, 1)
    out = out.replace(ANCHOR_HELPERS, HELPERS + ANCHOR_HELPERS, 1)
    out = out.replace(CALL_OLD, CALL_NEW, 1)
    out = out.replace(FETCH_OLD, FETCH_NEW, 1)

    with open(APP, "w", encoding="utf-8") as fp:
        fp.write(out)

    print("backup:", bak)
    print("new md5:", hashlib.md5(out.encode()).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
