# Maimai Static BGA

将 maimai DX 数据包中的歌曲封面转换为体积较小的静态 BGA，为轻量资源包补齐缺失背景，或用静态封面替换已有 PV

## 环境要求

- Python 3.11 或以上版本。
- FFmpeg，包含 `ffmpeg`、`ffprobe` 和 `libvpx-vp9` 编码器，且已加入 PATH。
- A000 内包含 `music`、`SoundData` 和 `AssetBundleImages/jacket`。

## 安装与配置

进入项目目录，创建虚拟环境并安装依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .envexample .env
```

Windows 可使用 `py -3 -m venv .venv` 创建环境、`.venv\Scripts\Activate.ps1` 激活环境，并使用 `Copy-Item .envexample .env` 复制配置。

编辑项目根目录的 `.env`：

```dotenv
DEFAULT_A000=/path/to/StreamingAssets/A000
DEFAULT_KEY=
```

| 配置项           | 说明                                                                                                                           |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `DEFAULT_A000` | A000 资源路径。相对路径以 `bga.py` 所在目录为基准；Windows 建议使用 `D:/maiServer/StreamingAssets/A000` 这样的正斜杠路径。 |
| `DEFAULT_KEY`  | 与目标数据版本匹配的 CRI USM 密钥。请自行填写，支持十进制或 `0x` 开头的十六进制无符号 64 位整数。生成和解密验证时需要。      |

配置优先级为：**命令行参数 > 同名系统环境变量 > `.env`**

## 使用方法

以下命令在项目根目录运行，并使用已激活的venv

### 扫描资源

```bash
python bga.py scan
```

检查歌曲、封面和音频的对应关系，将结果写入 `output/inventory.json`。发现缺失资源或不支持的音频格式时会报错。

### 生成全部 BGA

```bash
python bga.py build --workers 4
```

默认生成 **1080×1080、1 fps、音频时长加 2 秒**的静态视频。最终文件位于 `output/MovieData`，构建汇总位于 `output/build_report.json`。

相同命令可断点续跑。参数、来源和输出摘要均一致的文件会复用；需要重新生成时使用 `--force`。请保留 `output/records`，以便校验和续跑。

只生成指定视频或限制数量：

```bash
python bga.py build --ids 8 30
python bga.py build --limit 10
```

`--ids` 使用 `Music.xml` 的 **`movieName/id`**，示例编号请按自己的资源替换。

自定义输出参数：

```bash
python bga.py build --size 720 --fps 1 --crf 24 --output output-720
python bga.py build --a000 /another/StreamingAssets/A000 --workers 4
```

### 添加或替换 BGA

为 A000 补齐缺失的 BGA：

```bash
python bga.py install
```

只安装指定视频：

```bash
python bga.py install --ids 8 30
```

`install` 会校验生成物及来源，只写入目标中不存在的文件，并将新增文件记录到 `output/install_report*.json`。目标中已有同名文件会跳过。

**替换已有 PV 或重新安装另一组参数的 BGA**：先备份目标 `A000/MovieData`，再将 `output/MovieData` 中需要的 `.dat` 文件复制覆盖到目标目录。只覆盖生成的数字编号文件即可，无需替换整个目录。

`build --force` 仅重建输出目录中的文件，不会让 `install` 覆盖 A000 中的已有文件。

### 验证生成物

```bash
python bga.py verify output/MovieData/000008.dat
```

验证包括容器头、seek 偏移、解密、帧数和完整视频解码。每个新生成的文件也会自动验证，通过后才保存到最终输出目录。

包含日文文件名的原游戏容器可指定文本编码：

```bash
python bga.py verify /path/to/MovieData/example.dat --encoding cp932
```

## 命令与参数

```bash
python bga.py --help
python bga.py build --help
```

| 命令             | 作用                              |
| ---------------- | --------------------------------- |
| `scan`         | 扫描歌曲和关联资源。              |
| `build`        | 生成静态 BGA。                    |
| `install`      | 将生成物补入 A000，跳过已有文件。 |
| `verify <dat>` | 解密并验证单个 VP9/USM 文件。     |

| 参数                  | 适用命令                         | 默认值 / 说明                                                                         |
| --------------------- | -------------------------------- | ------------------------------------------------------------------------------------- |
| `--a000 PATH`       | `scan`、`build`、`install` | 读取 `DEFAULT_A000`。显式传入的相对路径以当前工作目录为基准。                       |
| `--output PATH`     | `scan`、`build`、`install` | 项目目录下的 `output`；显式相对路径以当前工作目录为基准。输出目录必须与 A000 分离。 |
| `--ids ID [ID ...]` | `build`、`install`           | 仅处理指定视频 ID。                                                                   |
| `--limit N`         | `build`、`install`           | 按视频 ID 升序选取前 N 个；可与 `--ids` 同用。                                      |
| `--size N`          | `build`                        | `1080`；输出为正方形，允许 64～4096 之间的偶数，保持封面比例。                      |
| `--fps N`           | `build`                        | `1`；允许 1～60。                                                                   |
| `--tail SECONDS`    | `build`                        | `2`；音频结束后的额外秒数，允许 0～60。                                             |
| `--crf N`           | `build`                        | `24`；允许 0～63，数值越低通常画质越高、体积越大。                                  |
| `--workers N`       | `build`                        | `2`；并行任务数，允许 1～16。                                                       |
| `--key KEY`         | `build`、`verify`            | 读取 `DEFAULT_KEY`；可传十进制或 `0x` 开头的十六进制值。                          |
| `--force`           | `build`                        | 允许替换输出目录中的已有生成物。                                                      |
| `--encoding NAME`   | `verify`                       | `utf-8`；原游戏容器可按需使用 `cp932`。                                           |

## 使用范围

当前处理单个 A000，音频支持单流 AFS2/HCA。输出分辨率不会增加原封面的真实细节。客户端兼容性和实际显示效果请先用少量歌曲验证。

本项目仅提供转换工具，使用时需自行准备对应游戏资源和密钥。
