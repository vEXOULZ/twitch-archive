import shutil
import subprocess

import pytest

from archive_worker import ffmpeg, hls

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """12 s test pattern with a tone, 1 s GOP so cuts land close to the target."""
    out = tmp_path_factory.mktemp("media") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
         "-t", "12", "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
        check=True,
    )
    return out


async def test_probe_and_cut(clip, tmp_path):
    assert abs(await ffmpeg.probe_duration(clip) - 12) < 0.2
    part = await ffmpeg.cut(clip, tmp_path / "p2.mp4", 5, 4)
    assert abs(await ffmpeg.probe_duration(part) - 4) < 1.1
    assert not (tmp_path / "p2.mp4.part").exists()


async def test_mute_and_blackout_keep_length(clip, tmp_path):
    muted = await ffmpeg.mute(clip, tmp_path / "m.mp4", [(1, 3), (6, 7)])
    assert abs(await ffmpeg.probe_duration(muted) - 12) < 0.3
    black = await ffmpeg.blackout(clip, tmp_path / "b.mp4", 4, 6, tmp_path / "work")
    assert abs(await ffmpeg.probe_duration(black) - 12) < 1.1
    assert not list((tmp_path / "work").glob("bo-*"))


async def test_hls_to_mp4(clip, tmp_path):
    seg_dir = tmp_path / "hls"
    seg_dir.mkdir()
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip), "-c", "copy",
         "-f", "segment", "-segment_time", "4", "-segment_format", "mpegts", str(seg_dir / "%d.ts")],
        check=True,
    )
    names = sorted((p.name for p in seg_dir.glob("*.ts")), key=lambda n: int(n.split(".")[0]))
    (seg_dir / "index.m3u8").write_text(hls.write_local_playlist([(n, 4.0, False) for n in names]))
    out = await ffmpeg.hls_to_mp4(seg_dir / "index.m3u8", tmp_path / "out.mp4")
    assert abs(await ffmpeg.probe_duration(out) - 12) < 0.5


async def test_failure_leaves_no_output(tmp_path):
    with pytest.raises(ffmpeg.FfmpegError):
        await ffmpeg.cut(tmp_path / "missing.mp4", tmp_path / "x.mp4", 0, 1)
    assert not (tmp_path / "x.mp4").exists()
