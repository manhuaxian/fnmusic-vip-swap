# fnmusic-vip-swap

给 [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext)（飞牛音乐在线曲库扩展）用的一个小补丁。

**解决的问题**：用没有 VIP 权益的网易账号，歌单里的 VIP 曲目只能听 30~60 秒（2.9.0 已经在歌名前加了 `[VIP]` 前缀提醒你）。插件本身不做跨音源换歌，所以听到一半就断了。

**这个补丁做的事**：播放时认出 VIP 试听曲目 → 用「歌名 歌手」去你配好的洛雪音源搜同曲完整版 → 命中就换源播完整版，同时后台把完整版整轨存进本地曲库 → 下次再点直接走本地文件、秒开。命中不了就原样回落网易试听，**不会比原来更差**。

---

## 实测效果（我的 NAS）

| 歌曲 | 结果 |
|---|---|
| 你从未离去 - 白挺（VIP） | 换源成功，落库 40 MB FLAC，二次点开本地秒开 |
| The Other Side Of Paradise - Glass Animals（VIP） | 落库 37.7 MB |
| Brightest Light - Krisu（VIP） | 落库 29 MB |
| 左手右手 - 杨沛宜（VIP） | 落库 19.6 MB |
| 晴天 / 稻香 / 夜曲 - 周杰伦 | **失败**，回落试听（见下方「已知限制」） |

---

## 怎么装

前提：

- fnmusic-ext 已经装好，洛雪音源可用（`FNMUSIC_LX_ENABLED='true'`）
- 版本：针对 **2.9.0** 的代码结构写的。其他版本锚点可能对不上，脚本会自己报 `ANCHOR MISMATCH` 然后**不动任何东西**。

```bash
# 1) 进到 proxy 目录
cd /vol1/@appcenter/fnmusic-ext/repo/proxy

# 2) 备份一份（脚本打补丁时也会自动再备一份带时间戳的）
cp app.py app.py.orig

# 3) 把 vip_swap_patch.py 传上来（wget / curl / scp 都行），然后
python3 vip_swap_patch.py --check      # 先看锚点对不对，确认输出都是 1
python3 vip_swap_patch.py --apply      # 打补丁
python3 -m py_compile app.py           # 编译校验
systemctl restart fnmusic-ext.service  # 重启服务

# 4) 打开开关（默认就是 true，一般不用管）
grep -q FNMUSIC_VIP_SWAP /vol1/@appcenter/fnmusic-ext/repo/.env || \
  echo "FNMUSIC_VIP_SWAP='true'" >> /vol1/@appcenter/fnmusic-ext/repo/.env
```

装完点一首歌名前带 `[VIP]` 的歌试试：第一遍会顿 1~3 秒（在搜洛雪 + 起后台下载），听完这首它就已经在本地了，之后再点秒开。

> 安装路径不是默认位置时：`FNMUSIC_APP_PY=/your/path/proxy/app.py python3 vip_swap_patch.py --apply`

---

## 装之前：一定要先清掉残留的试听片段

**这是最容易踩的坑。**

如果你之前开过「边听边存」或「收藏自动绑定本地」，本地很可能已经存了一批 20~35 秒的试听片段。播放时 `find_cache_file()` 会**优先命中这些本地文件**，补丁根本没机会执行 —— 表现就是「打了补丁但还是只有几十秒」。

先用下面这段扫一遍（两个目录都要看）：

```bash
python3 - <<'EOF'
import os, subprocess
for root in ("/vol2/1000/music/music", "/vol2/1000/music/cache"):
    if not os.path.isdir(root):
        continue
    print("===", root, "===")
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isfile(p) or name.endswith((".ref", ".lrc")):
            continue
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p],
            capture_output=True, text=True).stdout.strip()
        try:
            sec = float(out)
        except ValueError:
            continue
        if sec < 90:
            print(f"  片段 {sec:6.1f}s  {p}")
EOF
```

把列出来的文件删掉（或先移到别处观察一两天），再测换源。片段被删掉之后，下次点这首歌就会重新走换源。

> 注意：`/vol2/1000/music/music` 是曲库目录，`/vol2/1000/music/cache` 是滚动缓存目录 —— 具体路径看你 `.env` 里的 `FNMUSIC_TEE_SAVE_DIR`。

---

## 开关与回滚

| 想做什么 | 怎么做 |
|---|---|
| 临时关掉 | `.env` 里把 `FNMUSIC_VIP_SWAP='true'` 改成 `'false'`，**热重载，不用重启** |
| 彻底还原 | `python3 vip_swap_patch.py --revert` 然后重启服务 |
| 用备份还原 | `cp app.py.bak.vipswap.<时间戳> app.py` 然后重启服务 |

---

## 匹配规则（为什么不会串歌）

1. 标题去掉空格、标点、括号后**完全相等** —— 第一轮；
2. 不够就放宽成**互相包含**（限制长度差 ≤6 字，防「年轮」/「年轮说」这类误配）—— 第二轮；
3. 两轮都要求**歌手名有交集**，把翻唱拦掉；
4. 候选时长 <60 秒的直接弃用；
5. 结果缓存 6 小时；搜不到缓存 15 分钟，过期后会自动重试。

所以歌名带不带 `(Live)`、`feat.` 之类的差异不影响，但歌手对不上的翻唱不会误配。

---

## 已知限制

- **源头没有的救不了**：周杰伦这类 JVR 独家曲目，洛雪能搜到条目但解析不出可播直链 → 会先试两次洛雪、失败后回落网易 30 秒试听（多等几秒，结果不变）。
- 只对**网易源**且带 VIP 标记的曲目生效，其他音源不受影响。
- 改的是插件 Python 源码，**插件升级（装新 fpk）会覆盖 `app.py`，补丁随之失效**。升级后重跑一次 `--apply` 就行（脚本会自己判断已打没打）。
- 换源走的是 307 跳转，代理不经手音频字节，所以播放路径的「边听边存」对换源曲目不会触发 —— 完整版落库由补丁另起的后台下载负责，这也是补丁里要单独接这一段的原因。
- 洛雪确实没有的 VIP 歌，播放时插件仍会把 30 秒试听写进滚动缓存（上游机制，滚动只保留最新 2 个）。

---

## 实现要点（给想改的人）

- 在 `stream_track` 构造候选列表的地方插入换源判定，复用插件**已有的**跨源候选 + 307 跳转机制（它本来就用这套在 musicdl 内部跨平台换源），改动面很小。
- `_full_fetch_download`（后台整轨下载 / 收藏自动绑定 / 边听边存兜底）里做同样的换源判断：**取流用洛雪 guid，落库仍用原网易 guid** —— 保证曲库命名、官方收藏绑定都还是按网易那首的元数据走。
- VIP 判定：优先看 `item["needs_vip"] is True`（2.9.0 用它动态给标题加 `[VIP]` 前缀，`_ONLINE_META` 里存的标题是干净的），同时兼容标题已带前缀的情况；两路都拿不到时，落到磁盘歌单缓存反查。
- 开关走 `.env` 的 `FNMUSIC_VIP_SWAP`，并且注册进 `_ENV_WATCH_KEYS` 白名单，这样才能热重载。
- 补丁是幂等的：`--apply` / `--revert` 可反复跑，锚点唯一性会在动手前校验。

---

## 免责

仅供个人自用学习交流。请遵守各音源的服务条款，不要用于分发或商业用途。

补丁脚本：[`vip_swap_patch.py`](./vip_swap_patch.py)
