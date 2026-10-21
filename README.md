# 拾光相册 / Lumina

本地照片回忆整理应用：选中一个文件夹，用本机的 OpenAI 兼容视觉模型自动打分、打标签、写简评，再以杂志式的画廊浏览、筛选、珍藏和搜索。照片是拍立得，视频是胶片，动态照片会动。

## 设计理念

- **像一本独立摄影杂志**：暖色纸张与胶片质感，留白与衬线排版，让整理旧照片这件事慢下来。
- **本地优先**：照片、评分和分析结果只保存在本机。唯一的联网行为是逆地理编码，且可以关掉。
- **原图只读**：程序不会修改原始照片和视频，删除只在明确确认后发生。

## 功能介绍

**照片**
- 扫描常见图片格式（JPG / PNG / GIF / BMP / WebP / TIFF，装了 `pillow-heif` 后还有 HEIC / AVIF），首次扫描建立索引并生成代理图
- 本地视觉模型分析：标题、总分、技术 / 回忆 / 独特三项评分、中文标签与简评
- 详情页：原图、相机参数（光圈 / 快门 / ISO / 焦距）、拍摄时间、地点、文件大小（有同名 RAW 时显示两个数值）、评分条、标签与简评
- 手机照片优先显示**等效 35mm 焦距**（4.2mm 的镜头写成 26mm，才是画面看起来的样子）
- 全屏查看：滚轮缩放、拖拽平移、双击切换
- RAW：同名 JPG 与 RAW 视为一张照片（JPG 优先）；只有 RAW 时用其内嵌预览生成缩略图
- 竖拍照片按 EXIF 方向正确显示，卡片和详情页都不会把竖构图横过来

**视频**
- 手机、相机、无人机拍的视频与照片混排在同一张网格里，卡片是**电影胶片样式**：黑色片基、两侧圆角矩形齿孔、时长角标
- 格式覆盖 MP4 / M4V / MOV / QT / MKV / WebM / AVI / WMV / FLV / MPG / MPEG / MTS / M2TS / TS / 3GP / 3G2 / OGV / DV / MOD / TOD / INSV / 360
- 浏览器放不了的容器会自动换成可播放副本：能流复制就不重编码（几乎瞬间），必须重编码时走后台任务
- 详情页播放一段预览，可选**不播放 / 播放一次 / 循环播放**，音量 0-100% 可拖动也可输入；左下角播放键在任何模式下都在，右上角扬声器随时开关声音
- 规格行用图标分别列出分辨率、编码、码率、帧率；时长与封装、音频各占一行
- 全屏播放**原片**：默认有声、可拖动进度
- 视频同样参与大模型评分——只把封面帧喂给模型，卡片和详情页照常显示评分与标签，也可以编辑

**动态照片 / 实况照片**
- 识别 Google Motion Photo、旧版 Micro Video、三星 MotionPhoto 与 iPhone 实况照片
- 卡片带 iOS 风格的同心圆角标，规格行里有一枚「Live」标记
- 打开详情页时**自动播放**那一段，播完淡回静止画面（和 iPhone 一样）；画面右下角的播放键可以重播
- 音量单独设置，默认 0%（静音）
- 设置里关掉「合并显示实况照片」，同名照片与实况视频会拆成两张卡片（下次重新整理时生效）

**通用**
- 筛选：类型（全部 / 照片 / 视频 / 实况）、最低分、标签、年份；按总分、拍摄时间、文件名、分析时间、文件大小排序
- 搜索：关键词、智能（关键词 + 语义）、语义
- 珍藏、暂时收起、恢复、彻底移除；回收站可一键清空
- 进度反馈：整理、同步、导入、清空回收站都会显示进度条与**预计剩余时间**；刷新页面不会打断任务
- 键盘快捷键：`←` `→` 切换、`F` 珍藏、`E` 编辑、`Esc` 关闭
- 从 SD 卡导入并保持同步：卡上删除可以同步到卡，卡不在时排队等下次插卡
- 设置与界面状态存在数据库里，换浏览器或清除浏览器记录都不会丢

## 安装

需要 **Python 3.9+**：

```bash
pip install -r requirements.txt
python app.py
```

浏览器打开 <http://127.0.0.1:5000>。

| 包 | 用途 |
| --- | --- |
| Flask | Web 服务 |
| Pillow | 照片解码、缩略图、代理图 |
| requests | 调用本地模型与地理编码 |
| pillow-heif（可选） | 解码 HEIC / AVIF 与 HEIC 版动态照片；没装也能跑，这些文件会被跳过 |

**视频与动态照片还需要 ffmpeg**（系统依赖，不是 Python 包）：

```bash
winget install Gyan.FFmpeg     # Windows
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Linux
```

没装时照片功能完全正常，视频只有占位卡片；设置页的「视频处理」页会显示检测结果。ffmpeg 不在 PATH 上时，用 `PHOTO_FFMPEG_PATH` 指向其目录。

最后启动任意兼容 OpenAI Chat Completions 且支持图片输入的本地模型服务（LM Studio、Ollama、vLLM 等），在右上角「设置」里填 API 地址、Key 和模型名。

## 使用

1. 在顶部输入或选择照片文件夹；路径框右侧的小箭头展开**整理过的文件夹**。
2. 点「开始整理」：首次扫描建立索引并生成代理图，随后逐张调用模型分析。勾选「重新整理全部」会先重新读取整个文件夹，再把每一项重新送交模型——按下按钮后会先弹窗说明后果，确认后才开始。
3. 用筛选、排序和搜索浏览；点击卡片看详情。
4. 文件夹有增删时点「同步相册」增量更新，不会重复调用模型。勾选「重新整理全部」时它只重读文件信息（大小、EXIF、地点、视频参数），同样会先弹窗确认。

从 SD 卡导入：先选中本地目标文件夹 → 点「从 SD 卡导入」→ 选卡上的目录 → 预演确认后开始。导入过一次之后，这个目录会被记为该卡的**相机相册**，顶栏显示卡片状态，删除照片时可以勾选同步删卡。

## 配置

数据默认在仓库根目录的 `data/`，数据库是 `data/library.db`。

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
| `PHOTO_INDEX_CONCURRENCY` | `4` | 建索引 / 生成代理图的线程数 |
| `PHOTO_REQUEST_TIMEOUT` | `120` | 模型请求超时（秒） |
| `PHOTO_MODEL_RETRIES` | `3` | 模型请求重试次数 |
| `PHOTO_DATA_DIR` | 仓库下的 `data` | 数据库目录 |
| `PHOTO_DB_PATH` | 空 | 直接指定数据库文件 |
| `PHOTO_CACHE_DIR_NAME` | `.photo-review-cache` | 代理图缓存目录名 |
| `PHOTO_TRASH_DIR_NAME` | `.photo-trash` | 收起区目录名 |
| `PHOTO_GEOCODING_PROVIDER` | `nominatim` | `nominatim` / `amap` / `none`（不联网，直接显示坐标） |
| `PHOTO_GEOCODING_API_KEY` | 空 | 高德 Key |
| `PHOTO_GEOCODING_INTERVAL` | `1.0` | 地理编码最小间隔（秒） |
| `PHOTO_GEOCODING_RETRIES` | `3` | 地理编码重试次数 |
| `PHOTO_FFMPEG_PATH` | 空 | ffmpeg/ffprobe 所在目录 |
| `PHOTO_VIDEO_CLIP_SECONDS` | `3` | 详情页预览时长 |
| `PHOTO_VIDEO_CLIP_MAX_EDGE` | `960` | 预览最大边长 |
| `PHOTO_VIDEO_CLIP_CRF` | `28` | 预览压缩质量 |
| `PHOTO_VIDEO_CLIP_AUDIO` | `true` | 预览是否带音轨 |
| `PHOTO_VIDEO_POSTER_RATIO` | `0.1` | 封面取帧位置（按时长比例） |
| `PHOTO_VIDEO_PREGENERATE` | `false` | 索引时预生成预览 |
| `PHOTO_VIDEO_PREVIEW_MODE` | `once` | 预览模式：`off` / `once` / `loop` |
| `PHOTO_VIDEO_PREVIEW_VOLUME` | `50` | 预览音量（0-100） |
| `PHOTO_VIDEO_PROCESSING` | `ffmpeg` | 视频处理方式：`ffmpeg` 读取视频；`none` 让之后的同步移除视频索引与其缓存（原文件不动）。重新同步后生效 |
| `PHOTO_LIVE_VOLUME` | `0` | 实况短片音量（0-100），默认静音 |
| `PHOTO_MERGE_LIVE_PHOTOS` | `true` | 合并显示实况照片；关闭后重新整理生效 |
| `PHOTO_HOST` | `127.0.0.1` | 监听地址 |
| `PHOTO_PORT` | `5000` | 监听端口 |
| `PHOTO_DEBUG` | `false` | Flask 调试模式 |
| `PHOTO_ALLOWED_ORIGINS` | 空 | 额外允许的写接口来源，逗号分隔 |

设置弹窗按类别分成五个标签页，保存后写进数据库：

- **大模型**：API 地址、Key、模型名、系统提示词（活泼 / 文艺 / 幽默 / 温暖 / 伤感预设）、并发数、重试次数、超时
- **图片处理**：代理图与缩略图的边长和质量、索引线程数、合并显示实况照片、实况音量
- **视频处理**：处理方式（ffmpeg / 不处理）、预览时长、最大边长、CRF、取帧位置、预览模式、预览音量、是否带音轨、是否索引时预生成、ffmpeg 检测状态
- **地理编码**：服务、Key、请求间隔、重试次数
- **数据管理**：移除当前目录、清理缓存

`PHOTO_HOST` / `PHOTO_PORT` / `PHOTO_DEBUG` / `PHOTO_FFMPEG_PATH` 只从环境变量读取，不会被数据库里的旧值覆盖。

## 已知限制

- HDR 视频的封面帧可能偏灰（未做色调映射）
- Insta360 的 `.insv` / `.360` 能索引，但播放需要先拼接，直接播是鱼眼画面
- 视频只把封面帧送给模型，评分反映的是那一帧，不是整段
