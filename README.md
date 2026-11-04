# 拾光相册 / Lumina

本地照片回忆整理应用。选一个文件夹，用本机的视觉模型给每张照片打分、打标签、写一句简评，再以杂志式的画廊翻阅、筛选、珍藏和搜索。照片是拍立得，视频是胶片，实况照片会动。

- **本地优先**：照片、评分、分析结果都在本机。唯一的联网行为是逆地理编码，可以关掉。
- **原图只读**：程序不修改原始照片和视频，删除只在确认后发生，且先进回收站。

## 功能介绍

**整理**
- 扫描 JPG / PNG / GIF / BMP / WebP / TIFF，装了 `pillow-heif` 还有 HEIC / AVIF
- 本地视觉模型分析：标题、总分、技术 / 回忆 / 独特三项评分、中文标签、一句简评
- 同名 RAW 与 JPG 视为一张照片；只有 RAW 时用内嵌预览生成缩略图
- 竖拍照片按 EXIF 方向正确显示

**浏览**
- 筛选：类型、最低分、标签、年份；排序：总分、拍摄时间、文件名、分析时间、文件大小
- 搜索四种方式：关键词、智能（关键词 + 语义）、大模型语义、**看图搜索**
- 详情页：原图、相机参数、拍摄时间、地点、评分条、标签与简评；全屏可缩放拖拽
- 键盘：`←` `→` 切换、`F` 珍藏、`E` 编辑、`Esc` 关闭
- 画廊拉到底会继续加载，也可以点底部的「展开更多回忆」

**那年今日**
- 按年分组展示往年同一天的照片，标题写成「一年前的今天」
- 时间范围可调（只看同一天，或前后 1–3 天）
- 已由大模型解读过的照片带一枚小标记
- 「一键整理」把这一天还没解读的照片交给模型，进度条在顶栏
- 「重建回忆」把这一天收成网页或长图，带模型写的标题与评语

**重复回忆**
- 用已有的 dHash 找出看起来重复的照片，独立页面分组浏览
- 勾选由复选框负责，点照片是看照片；详情页的上一张 / 下一张在该组内轮换
- 建议保留的那张也可以选；一组全选时标题会变红提示
- 相似程度可调，并记住上次的选择，收起后进回收站

**足迹**
- 按城市聚合照片，点进去是这个地点自己的照片墙
- 详情页的上一张 / 下一张在该地点的照片里轮换
- 不用地图：本地应用不该为此联网

**导出**
- 存成**网页**：单个 HTML，图片全部内嵌，十年后还能直接打开、也能打印
- 存成**长图**：一张竖版 PNG，适合直接发到社交平台

**视频与实况照片**
- 视频、照片混排；放不了的容器自动换可播放副本（能流复制就不重编码）
- 详情页预览可选不播放 / 播放一次 / 循环，音量可调
- 实况照片自动播放一次后淡回静止画面，右下角播放键重播
- 视频同样参与评分（只把封面帧喂给模型）

**SD 卡**
- 导入时保持卡片目录结构，导入后记为「相机相册」
- 按**卷标识**认卡（卷 GUID / UUID），换盘符、换读卡器仍是同一张卡
- 卡不在时删除请求排队，插卡后一并同步

**其他**
- 进度条与预计剩余时间，刷新页面不打断任务
- 多选可后台批量「重新解读」，可取消；模型连不上会立刻拦下
- 启动时自动备份数据库，保留最近 5 份（`data/backups/`）
- 设置与界面状态存在数据库里

## 安装

需要 **Python 3.9+**：

```bash
pip install -r requirements.txt
python app.py
```

启动后会自动用默认浏览器打开；不想自动打开就设 `PHOTO_OPEN_BROWSER=0`。端口被占用时会自动往后找一个可用端口并打印实际地址。

| 包 | 用途 |
| --- | --- |
| Flask | Web 服务 |
| Pillow | 照片解码、缩略图、代理图、长图导出 |
| requests | 调用本地模型与地理编码 |
| pillow-heif（可选） | 解码 HEIC / AVIF；没装也能跑，这些文件会被跳过 |

**视频与实况照片需要 ffmpeg**（系统依赖）：

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Linux
```

没装时照片功能完全正常，视频只有占位卡片。ffmpeg 不在 PATH 上时用 `PHOTO_FFMPEG_PATH` 指向其目录。

最后启动任意兼容 OpenAI Chat Completions 且支持图片输入的本地模型服务（LM Studio、Ollama、vLLM 等），在设置里填 API 地址、Key 和模型名。

## 使用

1. 顶部输入或选择照片文件夹；路径框右侧的小箭头展开**整理过的文件夹**。
2. 点「开始整理」：首次扫描建索引并生成代理图，随后逐张调用模型。勾选「重新整理全部」会重读整个文件夹并把每一项重新送交模型，按下后会先弹窗说明后果。
3. 用筛选、排序、搜索浏览；点击卡片看详情。
4. 文件夹有增删时点「同步相册」增量更新，不会重复调用模型。
5. 顶栏的「那年今日」「重复回忆」「足迹」是三个独立入口；多选后可以「导出回忆」。

**从 SD 卡导入**：先选中本地目标文件夹 → 点「从 SD 卡导入」→ 选卡上的目录 → 预演确认后开始。

## 配置

数据默认在 `data/`，数据库是 `data/library.db`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PHOTO_API_BASE_URL` | `http://localhost:1234/v1` | 模型 API 地址 |
| `PHOTO_API_KEY` | `not-needed` | API Key |
| `PHOTO_MODEL` | `local-model` | 模型名称 |
| `PHOTO_SYSTEM_PROMPT` | 空 | 自定义系统提示词（留空用内置） |
| `PHOTO_PROXY_MAX_EDGE` | `1280` | 代理图最大边长 |
| `PHOTO_GALLERY_THUMB_SIZE` | `640` | 画廊缩略图边长 |
| `PHOTO_PROXY_QUALITY` | `80` | 代理图 JPEG 质量 |
| `PHOTO_THUMB_QUALITY` | `50` | 缩略图 JPEG 质量 |
| `PHOTO_SCAN_CONCURRENCY` | `1` | 同时分析的照片数 |
| `PHOTO_INDEX_CONCURRENCY` | `4` | 建索引线程数 |
| `PHOTO_REQUEST_TIMEOUT` | `120` | 模型请求超时（秒） |
| `PHOTO_MODEL_RETRIES` | `3` | 模型请求重试次数 |
| `PHOTO_DATA_DIR` | `data` | 数据库目录 |
| `PHOTO_DB_PATH` | 空 | 直接指定数据库文件 |
| `PHOTO_CACHE_DIR_NAME` | `.photo-review-cache` | 代理图缓存目录名 |
| `PHOTO_TRASH_DIR_NAME` | `.photo-trash` | 收起区目录名 |
| `PHOTO_GEOCODING_PROVIDER` | `nominatim` | `nominatim` / `amap` / `none` |
| `PHOTO_GEOCODING_API_KEY` | 空 | 高德 Key |
| `PHOTO_GEOCODING_INTERVAL` | `1.0` | 地理编码最小间隔（秒） |
| `PHOTO_GEOCODING_RETRIES` | `3` | 地理编码重试次数 |
| `PHOTO_FFMPEG_PATH` | 空 | ffmpeg/ffprobe 所在目录 |
| `PHOTO_VIDEO_CLIP_SECONDS` | `3` | 详情页预览时长 |
| `PHOTO_VIDEO_CLIP_MAX_EDGE` | `960` | 预览最大边长 |
| `PHOTO_VIDEO_CLIP_CRF` | `28` | 预览压缩质量 |
| `PHOTO_VIDEO_CLIP_AUDIO` | `true` | 预览是否带音轨 |
| `PHOTO_VIDEO_POSTER_RATIO` | `0.1` | 封面取帧位置 |
| `PHOTO_VIDEO_PREGENERATE` | `false` | 索引时预生成预览 |
| `PHOTO_VIDEO_PREVIEW_MODE` | `once` | `off` / `once` / `loop` |
| `PHOTO_VIDEO_PREVIEW_VOLUME` | `50` | 预览音量（0-100） |
| `PHOTO_VIDEO_PROCESSING` | `ffmpeg` | `ffmpeg` 读视频；`none` 移除视频索引（原文件不动） |
| `PHOTO_LIVE_VOLUME` | `0` | 实况短片音量（0-100） |
| `PHOTO_MERGE_LIVE_PHOTOS` | `true` | 合并显示实况照片 |
| `PHOTO_MEMORY_WINDOW_DAYS` | `0` | 「那年今日」时间范围（0 = 只看同一天） |
| `PHOTO_OPEN_BROWSER` | `1` | 启动时自动打开浏览器 |
| `PHOTO_HOST` | `127.0.0.1` | 监听地址 |
| `PHOTO_PORT` | `5050` | 监听端口 |
| `PHOTO_DEBUG` | `false` | Flask 调试模式 |
| `PHOTO_ALLOWED_ORIGINS` | 空 | 额外允许的写接口来源，逗号分隔 |

设置弹窗分五个标签页，保存后写进数据库：

- **大模型**：API 地址、Key、模型名、系统提示词、并发数、重试次数、超时
- **图片处理**：代理图与缩略图、索引线程数、实况照片、那年今日的时间范围（最多前后 3 天）
- **视频处理**：处理方式、预览参数、ffmpeg 检测状态
- **地理编码**：服务、Key、请求间隔、重试次数
- **数据管理**：移除当前目录、清理缓存

`PHOTO_HOST` / `PHOTO_PORT` / `PHOTO_DEBUG` / `PHOTO_FFMPEG_PATH` / `PHOTO_OPEN_BROWSER` 只从环境变量读取。

## 已知限制

- HDR 视频的封面帧可能偏灰（未做色调映射）
- Insta360 的 `.insv` / `.360` 能索引，但直接播放是鱼眼画面
- 视频只把封面帧送给模型，评分反映那一帧，不是整段
- 「看图搜索」每次最多看 24 张，先用筛选缩小范围效果更好
- 「重复回忆」用的是 64 位 dHash，同场景的连拍可能被归为一组，相似程度可调
