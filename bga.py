#!/usr/bin/env python3

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

from dotenv import dotenv_values

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "output"
SCHEMA = 1


def read_config() -> dict[str, str]:
    """读取脚本旁的 .env；同名系统环境变量优先，不修改进程环境。"""
    values = dotenv_values(HERE / ".env", interpolate=False)
    return {name: (os.environ.get(name, values.get(name)) or "").strip()
            for name in ("DEFAULT_A000", "DEFAULT_KEY")}


def parse_key(value: str) -> int:
    """将十进制或 0x 开头的十六进制密钥转换为无符号 64 位整数。"""
    try:
        value = value.strip()
        key = int(value, 16 if value.lower().startswith("0x") else 10)
    except ValueError as exc:
        raise ValueError("密钥必须是十进制整数或 0x 开头的十六进制整数") from exc
    if not 0 <= key < 2**64:
        raise ValueError("密钥必须在无符号 64 位整数范围内")
    return key


@dataclass(frozen=True)
class Song:
    """同时记录music_id, movie_id, cur_id，因为不能保证它们一样，用三组id关联一首歌曲的信息"""

    music_id: int
    movie_id: int
    cue_id: int
    title: str
    xml: str


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False, suffix=".tmp") as f:
        temporary = Path(f.name)
        try:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def load_songs(a000: Path) -> list[Song]:
    """读A000/music"""
    if not (a000 / "music").is_dir():
        raise ValueError(f"不是有效 A000，缺少 music：{a000}")
    result = []
    for path in sorted((a000 / "music").glob("*/Music.xml")):
        root = ET.parse(path).getroot()
        try:
            result.append(Song(int(root.findtext("name/id", "")),
                               int(root.findtext("movieName/id", "")),
                               int(root.findtext("cueName/id", "")),
                               root.findtext("name/str", ""), str(path)))
        except ValueError as exc:
            raise ValueError(f"歌曲 ID 解析失败：{path}") from exc
    if not result:
        raise ValueError(f"没有找到 Music.xml：{a000}")
    return result


def read_exact(f, length: int) -> bytes:
    data = f.read(length)
    if len(data) != length:
        raise ValueError("!!!")
    return data


def awb_duration(path: Path) -> float:
    """读取 AFS2/HCA 头，不解密或解码音频
    """
    size = path.stat().st_size
    with path.open("rb") as f:
        header = read_exact(f, 16)
        if header[:4] != b"AFS2":
            raise ValueError(f"不是 AFS2：{path}")
        offset_bytes, id_bytes = header[5], int.from_bytes(header[6:8], "little")
        count = int.from_bytes(header[8:12], "little")
        alignment = int.from_bytes(header[12:14], "little")
        if count != 1:
            raise ValueError(f"需要单流 AWB，实际 {count} ：{path}")
        if offset_bytes not in (2, 4, 8) or id_bytes not in (2, 4) or not alignment:
            raise ValueError(f"不支持的 AFS2 头：{path}")
        table_end = 16 + id_bytes * count + offset_bytes * (count + 1)
        f.seek(16 + id_bytes * count)
        start = int.from_bytes(read_exact(f, offset_bytes), "little")
        end = int.from_bytes(read_exact(f, offset_bytes), "little")
        start = (start + alignment - 1) // alignment * alignment
        if start < table_end or not start < end <= size:
            raise ValueError(f"AFS2 偏移越界：{path}")
        f.seek(start)
        hca = read_exact(f, 24)
        tag = lambda data: bytes(x & 0x7F for x in data)
        if tag(hca[:4]) != b"HCA\0" or tag(hca[8:12]) != b"fmt\0":
            raise ValueError(f"音频不是支持的 HCA/fmt：{path}")
        data_offset = int.from_bytes(hca[6:8], "big")
        channels = hca[12]
        sample_rate = int.from_bytes(hca[13:16], "big")
        blocks = int.from_bytes(hca[16:20], "big")
        delay, padding = struct.unpack(">HH", hca[20:24])
        samples = blocks * 1024 - delay - padding
        if not (1 <= channels <= 16 and 8000 <= sample_rate <= 384000
                and samples > 0 and 24 <= data_offset <= end - start):
            raise ValueError(f"不合理的 HCA 参数：{path}")
        # comp/dec 的前两个字节是块长；同时检查音频载荷是否完整。
        chunk = read_exact(f, 6)
        if tag(chunk[:4]) not in (b"comp", b"dec\0"):
            raise ValueError(f"缺少 HCA comp/dec：{path}")
        block_size = int.from_bytes(chunk[4:6], "big")
        if not block_size or data_offset + blocks * block_size > end - start:
            raise ValueError(f"HCA 被截断：{path}")
        return samples / sample_rate


def inventory(a000: Path) -> dict:
    """按视频 ID 去重"""
    songs = load_songs(a000)
    groups: dict[int, list[Song]] = {}
    for song in songs:
        if song.movie_id > 0:
            groups.setdefault(song.movie_id, []).append(song)
    movies, errors = [], []
    for movie_id, references in sorted(groups.items()):
        # 优先同名封面；其次使用引用此视频的歌曲封面
        candidates = [movie_id] + sorted({s.music_id for s in references})
        jacket = next((a000 / "AssetBundleImages" / "jacket" / f"ui_jacket_{i:06d}.ab"
                       for i in candidates
                       if (a000 / "AssetBundleImages" / "jacket" / f"ui_jacket_{i:06d}.ab").is_file()), None)
        if jacket is None:
            errors.append({"movie_id": movie_id, "error": "缺少封面"})
            continue
        audio = []
        try:
            for cue_id in sorted({s.cue_id for s in references}):
                awb = a000 / "SoundData" / f"music{cue_id:06d}.awb"
                audio.append({"cue_id": cue_id, "path": str(awb),
                              "duration": awb_duration(awb)})
        except (OSError, ValueError) as exc:
            errors.append({"movie_id": movie_id, "error": str(exc)})
            continue
        movies.append({"movie_id": movie_id, "jacket": str(jacket),
                       "duration": max(a["duration"] for a in audio), "audio": audio,
                       "songs": [asdict(s) for s in references],
                       "existing_movie": (a000 / "MovieData" / f"{movie_id:06d}.dat").exists()})
    return {"schema": SCHEMA, "a000": str(a000), "song_count": len(songs),
            "movie_count": len(groups), "ready_count": len(movies),
            "no_movie_songs": [asdict(s) for s in songs if s.movie_id <= 0],
            "errors": errors, "movies": movies}


def run(command: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{command[0]} exit code {result.returncode}：\n{result.stderr[-6000:]}")
    return result


def require_tools() -> None:
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(name):
            raise RuntimeError(f"PATH 中找不到 {name}，请先安装 FFmpeg")
    encoders = run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
    if "libvpx-vp9" not in encoders:
        raise RuntimeError("FFmpeg 缺少 libvpx-vp9 编码器")
    try:
        import UnityPy  # noqa: F401
        from wannacri import usm  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("请使用 .venv/bin/python，并先安装 requirements.txt") from exc


def extract_jacket(bundle: Path, png: Path) -> tuple[int, int]:
    """优先提取 Sprite；无 Sprite 时才读取 Texture2D，保留正确裁切"""
    import UnityPy
    from PIL import Image

    env = UnityPy.load(str(bundle))
    for type_name in ("Sprite", "Texture2D"):
        objects = sorted((o for o in env.objects if o.type.name == type_name),
                         key=lambda o: o.path_id)
        if objects:
            images = [o.read().image for o in objects]
            image = max(images, key=lambda im: im.width * im.height).convert("RGBA")
            background = Image.new("RGBA", image.size, (0, 0, 0, 255))
            background.alpha_composite(image)
            background.convert("RGB").save(png)
            return image.size
    raise ValueError(f"AssetBundle 中没有 Sprite 或 Texture2D：{bundle}")


def probe_ivf(path: Path) -> dict:
    data = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                           "-count_frames", "-show_streams", "-of", "json", str(path)]).stdout)
    if not data.get("streams"):
        raise ValueError(f"No video stream found：{path}")
    stream = data["streams"][0]
    return {"codec": stream["codec_name"], "width": stream["width"],
            "height": stream["height"], "fps": str(stream["r_frame_rate"]),
            "frames": int(stream["nb_read_frames"]),
            "duration": int(stream["nb_read_frames"]) / float(Fraction(stream["r_frame_rate"])),
            "pixel_format": stream.get("pix_fmt"), "color_space": stream.get("color_space")}


def container_chunks(dat: Path, encoding: str = "utf-8"):
    """注意这里还要严格检查长度"""
    from wannacri.usm.chunk import UsmChunk

    size = dat.stat().st_size
    with dat.open("rb") as f:
        while f.tell() < size:
            position = f.tell()
            header = read_exact(f, 32)
            length = int.from_bytes(header[4:8], "big") + 8
            if length < 32 or position + length > size:
                raise ValueError(f"USM 块越界：{dat} @ {position}")
            raw = header + read_exact(f, length - 32)
            yield position, raw, UsmChunk.from_bytes(raw, encoding=encoding)


def normalize_container(dat: Path) -> None:
    """修正 WannaCRI 0.3.3 的两个头字段，保持各块长度及加密载荷不变。

    CRID.filesize 应等于实际容器大小；VIDEO_HDRINFO.metadata_size 应等于
    对应 METADATA 块总长（含块头和 padding），不是关键帧数量。
    不改变任何 seek 偏移，也不重新编码或重新加密视频。
    """
    from wannacri.usm.types import PayloadType, ChunkType

    prefix, metadata = [], {}
    for position, raw, chunk in container_chunks(dat):
        if chunk.payload_type == PayloadType.STREAM:
            break
        prefix.append((position, raw, chunk))
        if chunk.chunk_type == ChunkType.VIDEO and chunk.payload_type == PayloadType.METADATA:
            metadata[chunk.channel_number] = metadata.get(chunk.channel_number, 0) + len(raw)
    patches = []
    for position, raw, chunk in prefix:
        changed = False
        if chunk.chunk_type == ChunkType.INFO:
            for page in chunk.payload:
                if page["chno"].val == -1:
                    page.update("filesize", page["filesize"].type, dat.stat().st_size)
                    changed = True
        if chunk.chunk_type == ChunkType.VIDEO and chunk.payload_type == PayloadType.HEADER:
            page = chunk.payload[0]
            page.update("metadata_size", page["metadata_size"].type, metadata[chunk.channel_number])
            changed = True
        if changed:
            packed = chunk.pack()
            if len(packed) != len(raw):
                raise ValueError("refuse to write block data dur to overflow!")
            patches.append((position, packed))
    with dat.open("r+b") as f:
        for position, packed in patches:
            f.seek(position)
            f.write(packed)


def check_container(dat: Path, encoding: str = "utf-8") -> dict:
    from wannacri.usm.types import PayloadType, ChunkType

    chunks = list(container_chunks(dat, encoding))
    root = next(page for _, _, c in chunks if c.chunk_type == ChunkType.INFO
                for page in c.payload if page["chno"].val == -1)
    if root["filesize"].val != dat.stat().st_size:
        raise ValueError(f"CRID 文件大小与实际不符：{dat}")
    frames = [(offset, c) for offset, _, c in chunks
              if c.chunk_type == ChunkType.VIDEO and c.payload_type == PayloadType.STREAM]
    headers = [c for _, _, c in chunks
               if c.chunk_type == ChunkType.VIDEO and c.payload_type == PayloadType.HEADER]
    for header in headers:
        channel = header.channel_number
        channel_frames = [offset for offset, c in frames if c.channel_number == channel]
        meta = [(raw, c) for _, raw, c in chunks if c.chunk_type == ChunkType.VIDEO
                and c.payload_type == PayloadType.METADATA and c.channel_number == channel]
        if header.payload[0]["total_frames"].val != len(channel_frames):
            raise ValueError("USM 帧数与 VIDEO_HDRINFO 不符")
        if header.payload[0]["metadata_size"].val != sum(len(raw) for raw, _ in meta):
            raise ValueError("USM metadata_size 与实际块长度不符")
        if header.payload[0]["metadata_count"].val != len(meta):
            raise ValueError("USM metadata_count 不符")
        for _, chunk in meta:
            for page in chunk.payload:
                frame = page["ofs_frmid"].val
                if not 0 <= frame < len(channel_frames) or page["ofs_byte"].val != channel_frames[frame]:
                    raise ValueError("USM seek 偏移没有指向对应视频帧")
    return {"bytes": dat.stat().st_size, "frames": len(frames), "headers_valid": True,
            "video_channels": sorted({c.channel_number for _, c in frames}),
            "has_audio": any(c.chunk_type == ChunkType.AUDIO for _, _, c in chunks),
            "has_alpha": any(c.chunk_type == ChunkType.ALPHA for _, _, c in chunks)}


def verify_dat(dat: Path, key: int, scratch: Path, expected_hash: str | None = None,
               encoding: str = "utf-8") -> dict:
    from wannacri.usm.tools import generate_keys, decrypt_video_packet
    from wannacri.usm.types import PayloadType, ChunkType

    container_info = check_container(dat, encoding)
    if (len(container_info["video_channels"]) != 1 or container_info["has_audio"]
            or container_info["has_alpha"]):
        raise ValueError(f"要求单视频且无音轨：{dat}")
    # 只读取视频流；固定输出路径，不使用容器内的文件名写文件
    # 不调用 Usm.open：0.3.3 把文件句柄藏在闭包中且没有 close 接口
    video_key, _audio_key = generate_keys(key)
    ivf = scratch / "decrypted.ivf"
    with ivf.open("wb") as f:
        for _, _, chunk in container_chunks(dat, encoding):
            if chunk.chunk_type == ChunkType.VIDEO and chunk.payload_type == PayloadType.STREAM:
                f.write(decrypt_video_packet(chunk.payload, video_key))
    if expected_hash is not None and sha256(ivf) != expected_hash:
        raise ValueError(f"USM 加解密往返校验失败：{dat}")
    info = probe_ivf(ivf)
    if info["codec"] != "vp9" or info["frames"] <= 0:
        raise ValueError(f"无效的 VP9 视频：{dat}")
    run(["ffmpeg", "-v", "error", "-xerror", "-threads", "1", "-i", str(ivf),
         "-f", "null", "-"])
    return {**info, "container": container_info}


def settings(args) -> dict:
    from importlib.metadata import version

    return {"schema": SCHEMA, "size": args.size, "fps": args.fps, "crf": args.crf,
            "tail_seconds": args.tail, "key": args.key, "keyframe_seconds": 300,
            "container_revision": 2,
            "unitypy": version("UnityPy"), "wannacri": version("WannaCRI")}


def build_one(job: dict, config: dict, output: Path, force: bool) -> dict:
    from wannacri import usm

    movie_id = job["movie_id"]
    name = f"{movie_id:06d}"
    destination = output / "MovieData" / f"{name}.dat"
    record_path = output / "records" / f"{name}.json"
    fingerprint = hashlib.sha256(json.dumps({"settings": config,
        "jacket_sha256": sha256(Path(job["jacket"])), "duration": job["duration"]},
        sort_keys=True).encode()).hexdigest()
    if destination.exists() and not force:
        if record_path.is_file():
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if record.get("fingerprint") == fingerprint and record.get("sha256") == sha256(destination):
                return {**record, "status": "resumed"}
        raise FileExistsError(f"输出已存在但记录或参数不匹配；使用另一输出目录或 --force：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_root = output / ".tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{name}-", dir=temp_root) as tmp:
        tmp = Path(tmp)
        png, ivf, dat = tmp / "jacket.png", tmp / f"{name}.ivf", tmp / f"{name}.dat"
        original_size = extract_jacket(Path(job["jacket"]), png)
        frame_count = math.ceil((job["duration"] + config["tail_seconds"]) * config["fps"])
        size = config["size"]
        # 源图只缩放一次；后续静态帧由编码器预测压缩
        # 兼容一下smpte170m
        filters = (f"scale={size}:{size}:force_original_aspect_ratio=decrease:flags=lanczos:"
                   "out_color_matrix=smpte170m:out_range=tv,"
                   f"pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-loop", "1", "-framerate", str(config["fps"]), "-i", str(png),
             "-an", "-vf", filters, "-c:v", "libvpx-vp9", "-threads", "1",
             "-deadline", "good", "-cpu-used", "4", "-crf", str(config["crf"]),
             "-b:v", "0", "-g", str(config["fps"] * config["keyframe_seconds"]), "-row-mt", "1",
             "-tile-rows", "2", "-tile-columns", "2", "-colorspace", "smpte170m",
             "-color_range", "tv", "-r", str(config["fps"]),
             "-frames:v", str(frame_count), "-f", "ivf", str(ivf)])
        video = usm.Vp9(filepath=str(ivf))
        # WannaCRI 0.3.3 的 USM 默认不写 fmtver，而 Vp9 默认会写
        # 必须显式统一版本，否则 CRID 的两行列数不一致、无法封装
        stream = usm.Usm(videos=[video], key=config["key"],
                         version=video.crid_page["fmtver"].val)
        with dat.open("wb") as f:
            for packet in stream.stream(mode=usm.OpMode.ENCRYPT):
                f.write(packet)
        normalize_container(dat)
        info = verify_dat(dat, config["key"], tmp, sha256(ivf))
        if (info["frames"] != frame_count or info["width"] != size or info["height"] != size
                or Fraction(info["fps"]) != config["fps"] or info["duration"] < job["duration"]):
            raise ValueError(f"视频参数校验失败：{info}")
        record = {"movie_id": movie_id, "status": "built", "fingerprint": fingerprint,
                  "sha256": sha256(dat), "bytes": dat.stat().st_size,
                  "source_jacket": job["jacket"], "source_size": original_size,
                  "audio_duration": job["duration"], "settings": config,
                  "verification": info, "songs": job["songs"]}
        if force:
            os.replace(dat, destination)
        else:
            os.link(dat, destination)
        atomic_json(record_path, record)
        return record


def selected_jobs(plan: dict, ids: list[int] | None, limit: int | None) -> list[dict]:
    jobs = plan["movies"]
    if ids:
        wanted = set(ids)
        available = {j["movie_id"] for j in jobs}
        if wanted - available:
            raise ValueError(f"视频 ID not found：{sorted(wanted - available)}")
        jobs = [j for j in jobs if j["movie_id"] in wanted]
    return jobs[:limit] if limit else jobs


def build(args) -> int:
    require_tools()
    plan = inventory(args.a000)
    args.output.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output / "inventory.json", plan)
    if plan["errors"]:
        raise ValueError(f"有 {len(plan['errors'])} 项资源错误，详见 inventory.json")
    jobs = selected_jobs(plan, args.ids, args.limit)
    config = settings(args)
    results, errors = [], []
    start = time.monotonic()
    print(f"计划生成 {len(jobs)} 个视频，输出 {args.output / 'MovieData'}", flush=True)
    pool = ThreadPoolExecutor(max_workers=args.workers)
    cancelled = False
    try:
        futures = {pool.submit(build_one, j, config, args.output, args.force): j for j in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                record = future.result()
                results.append(record)
                if len(results) <= 3 or len(results) % 20 == 0 or len(results) + len(errors) == len(jobs):
                    print(f"[{len(results) + len(errors)}/{len(jobs)}] {job['movie_id']:06d} "
                          f"{record['status']} {record['bytes'] / 1024:.1f} KiB "
                          f"耗时 {time.monotonic() - start:.1f}s", flush=True)
            except Exception as exc:
                errors.append({"movie_id": job["movie_id"], "error": str(exc)})
                print(f"失败 {job['movie_id']:06d}：{exc}", file=sys.stderr, flush=True)
    except KeyboardInterrupt:
        cancelled = True
        print("KeyboardInterrupt", flush=True)
    finally:
        pool.shutdown(wait=True, cancel_futures=cancelled)
    report = {"schema": SCHEMA, "a000": str(args.a000), "settings": config,
              "requested": len(jobs), "succeeded": len(results), "failed": len(errors),
              "cancelled": cancelled,
              "bytes": sum(r["bytes"] for r in results),
              "elapsed_seconds": time.monotonic() - start, "errors": errors,
              "movies": sorted(results, key=lambda r: r["movie_id"])}
    atomic_json(args.output / "build_report.json", report)
    print(f"完成 {len(results)}/{len(jobs)}，总计 {report['bytes'] / 1024**2:.2f} MiB；"
          f"报告：{args.output / 'build_report.json'}", flush=True)
    return 130 if cancelled else (1 if errors else 0)


def install(args) -> int:
    plan = inventory(args.a000)
    if plan["errors"]:
        raise ValueError("源资源不完整，请先 scan")
    jobs = selected_jobs(plan, args.ids, args.limit)
    # 在写入 A000 前先验证全部待安装文件及其来源，避免漏生成却装到一半。
    entries = []
    for job in jobs:
        name = f"{job['movie_id']:06d}"
        target = args.a000 / "MovieData" / f"{name}.dat"
        if target.exists():
            continue
        source = args.output / "MovieData" / f"{name}.dat"
        record = json.loads((args.output / "records" / f"{name}.json").read_text(encoding="utf-8"))
        if sha256(source) != record["sha256"]:
            raise ValueError(f"生成物摘要不符：{source}")
        current = hashlib.sha256(json.dumps({"settings": record["settings"],
            "jacket_sha256": sha256(Path(job["jacket"])), "duration": job["duration"]},
            sort_keys=True).encode()).hexdigest()
        if current != record["fingerprint"]:
            raise ValueError(f"来源已变化，请重新 build：{source}")
        entries.append((source, target, record["sha256"]))
    installed = []
    journal = args.output / "install_report.json"
    # 每次保留独立日志，重复安装不会丢失上一次新增文件的清单。
    if journal.exists():
        journal = args.output / f"install_report_{time.time_ns()}.json"
    try:
        for source, target, digest in entries:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".static-bga-", delete=False) as f:
                temporary = Path(f.name)
            try:
                shutil.copyfile(source, temporary)
                if sha256(temporary) != digest:
                    raise ValueError(f"复制校验失败：{target}")
                os.link(temporary, target)
                installed.append({"path": str(target), "sha256": digest})
                atomic_json(journal, {"a000": str(args.a000), "installed": installed})
            finally:
                temporary.unlink(missing_ok=True)
    finally:
        atomic_json(journal, {"a000": str(args.a000), "installed": installed})
    print(f"新增 {len(installed)} 个 BGA；已有 {len(jobs) - len(entries)} 个保持原样。日志：{journal}")
    return 0


def main(argv: list[str] | None = None) -> int:
    config = read_config()
    default_a000 = Path(config["DEFAULT_A000"]).expanduser() if config["DEFAULT_A000"] else None
    if default_a000 is not None and not default_a000.is_absolute():
        default_a000 = HERE / default_a000
    parser = argparse.ArgumentParser(description="将 maimai 歌曲封面转换为静态 BGA（VP9/USM .dat）")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("scan", "build", "install"):
        p = sub.add_parser(command)
        p.add_argument("--a000", type=Path, default=default_a000,
                       help="A000 路径；默认读取 DEFAULT_A000")
        p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        if command != "scan":
            p.add_argument("--ids", type=int, nargs="+", help="按 movieName/id 选择视频")
            p.add_argument("--limit", type=int)
        if command == "build":
            p.add_argument("--size", type=int, default=1080)
            p.add_argument("--fps", type=int, default=1)
            p.add_argument("--tail", type=float, default=2.0, help="音频末尾的额外秒数")
            p.add_argument("--crf", type=int, default=24)
            p.add_argument("--workers", type=int, default=2)
            p.add_argument("--key", type=parse_key, help="CRI 密钥；默认读取 DEFAULT_KEY")
            p.add_argument("--force", action="store_true", help="允许重建输出目录中的文件")
    p = sub.add_parser("verify", help="解密 .dat 并完整解码，检查生成物或现有视频")
    p.add_argument("dat", type=Path)
    p.add_argument("--key", type=parse_key, help="CRI 密钥；默认读取 DEFAULT_KEY")
    p.add_argument("--encoding", default="utf-8", help="原游戏日文容器可使用 cp932")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    try:
        if args.command in ("build", "verify") and args.key is None:
            if not config["DEFAULT_KEY"]:
                raise ValueError("请在 .env 中设置 DEFAULT_KEY，或通过 --key 指定密钥")
            args.key = parse_key(config["DEFAULT_KEY"])
        if args.command == "verify":
            require_tools()
            with tempfile.TemporaryDirectory(prefix="static-bga-verify-") as tmp:
                print(json.dumps(verify_dat(args.dat.resolve(), args.key, Path(tmp),
                                           encoding=args.encoding), indent=2))
            return 0
        if args.a000 is None:
            raise ValueError("请在 .env 中设置 DEFAULT_A000，或通过 --a000 指定资源目录")
        args.a000, args.output = args.a000.expanduser().resolve(), args.output.expanduser().resolve()
        if args.output == args.a000 or args.a000 in args.output.parents or args.output in args.a000.parents:
            raise ValueError("输出目录必须与 A000 分离，请使用工具目录下的 output")
        if args.command == "scan":
            plan = inventory(args.a000)
            atomic_json(args.output / "inventory.json", plan)
            print(f"歌曲 {plan['song_count']}；视频 {plan['movie_count']}；"
                  f"就绪 {plan['ready_count']}；错误 {len(plan['errors'])}")
            print(f"清单：{args.output / 'inventory.json'}")
            return 1 if plan["errors"] else 0
        if args.limit is not None and args.limit <= 0:
            raise ValueError("--limit 必须大于 0")
        if args.command == "build":
            if not (64 <= args.size <= 4096 and args.size % 2 == 0):
                raise ValueError("--size 必须为 64 到 4096 之间的偶数")
            if not (1 <= args.fps <= 60 and 0 <= args.crf <= 63 and 0 <= args.tail <= 60
                    and 1 <= args.workers <= 16 and 0 <= args.key < 2**64):
                raise ValueError("参数范围：fps 1..60，crf 0..63，tail 0..60，workers 1..16，key uint64")
            return build(args)
        return install(args)
    except (OSError, ValueError, RuntimeError, KeyError, ET.ParseError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
