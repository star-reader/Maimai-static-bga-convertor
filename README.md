# Maimai Static BGA

将 maimai DX 歌曲封面转换为体积较小的静态 BGA，为轻量资源包补齐背景，或替换已有 PV。读取 A000 中的封面和音频信息，生成覆盖整首歌曲的加密 VP9/USM `.dat` 文件，无需原 PV 或 `option` 目录。

## 运行

需要 Python 3.11+ 和支持 `libvpx-vp9` 的 FFmpeg，并将 `ffmpeg`、`ffprobe` 加入 PATH。

在项目目录中安装依赖并创建配置。

**macOS / Linux：**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

**Windows（PowerShell）：**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

编辑 `.env`，填写资源目录 `DEFAULT_A000` 和对应版本的密钥 `DEFAULT_KEY`。Windows 路径可写为 `D:/maimai/StreamingAssets/A000`。

生成全部 BGA：

```bash
python bga.py build --workers 4
```

Windows 无需激活环境，将命令中的 `python` 换为 `.\.venv\Scripts\python.exe`，例如：

```powershell
.\.venv\Scripts\python.exe bga.py build --workers 4
```

默认生成 1080×1080、1 fps、音频时长加 2 秒的视频，输出至 `output/MovieData`。相同命令可断点续跑。

## 指令

- `python bga.py scan`：扫描歌曲、封面和音频资源。
- `python bga.py build`：生成静态 BGA，并自动校验。
- `python bga.py install`：将生成物补入 A000，跳过已有文件。
- `python bga.py verify <文件.dat>`：解密并验证单个视频。

替换已有 PV 时，先备份，再将生成的 `.dat` 复制覆盖到目标 `A000/MovieData`

更多参数使用 `python bga.py <指令> --help` 查看
