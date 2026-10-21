import os, io, base64, wave, time, subprocess, tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from sarvamai import SarvamAI
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def run_pipeline(
    images: list,
    texts: list,
    output_path: str,
    srt_path: str,
    api_key: str,
    speaker: str = "ishita",
    language: str = "en-IN",
    fps: int = 24,
    scale: str = "1920:1080",
    silence_pad: float = 0.4,
    draft: bool = True,
    fade_dur: float = 0.5,
    target_duration_s: float = None,   # optional: slow video to this many seconds
    max_audio_workers: int = 8,
    max_clip_workers: int = 4,
    max_retries: int = 3,
    progress_cb=None,
):
    def progress(pct, msg):
        if progress_cb:
            progress_cb(pct, msg)

    client = SarvamAI(api_subscription_key=api_key)

    def make_audio(text, out_path):
        for attempt in range(max_retries):
            try:
                audio = client.text_to_speech.convert(
                    text=text, model="bulbul:v3",
                    target_language_code=language, speaker=speaker,
                )
                with wave.open(out_path, "wb") as out:
                    first = True
                    for b64 in audio.audios:
                        with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
                            if first:
                                out.setparams(w.getparams()); first = False
                            out.writeframes(w.readframes(w.getnframes()))
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    raise RuntimeError(f"TTS failed for slide after {max_retries} attempts: {e}")

    with tempfile.TemporaryDirectory(prefix="vidpipeline_") as work_dir:
        # ── Phase 1: Generate audio ───────────────────────────────────────────
        progress(5, f"Generating narration for {len(texts)} slides…")
        wav_paths = [None] * len(texts)

        def gen_audio(args):
            idx, text = args
            wav = os.path.join(work_dir, f"audio_{idx:03d}.wav")
            make_audio(text, wav)
            return idx, wav

        with ThreadPoolExecutor(max_workers=max_audio_workers) as pool:
            futures = {pool.submit(gen_audio, (i, t)): i for i, t in enumerate(texts)}
            done = 0
            for future in as_completed(futures):
                idx, wav = future.result()
                wav_paths[idx] = wav
                done += 1
                progress(5 + int(done / len(texts) * 30), f"Audio {done}/{len(texts)} done")

        # ── Phase 2: Durations + SRT ──────────────────────────────────────────
        progress(36, "Building subtitle file…")

        def wav_duration(path):
            with wave.open(path, "rb") as w:
                return w.getnframes() / w.getframerate()

        # Full clip duration = speech + silence pad
        durations = [wav_duration(w) + silence_pad for w in wav_paths]

        # Compute slowdown factor so SRT timestamps are written correctly upfront
        total_s = sum(durations)
        if target_duration_s and target_duration_s > total_s:
            slow_factor = target_duration_s / total_s   # e.g. 1.22 for 7m49s → 9m30s
        else:
            slow_factor = 1.0

        def fmt_srt(s):
            h, rem = divmod(s, 3600)
            m, rem = divmod(rem, 60)
            sec = int(rem)
            ms = int((rem - sec) * 1000)
            return f"{int(h):02d}:{int(m):02d}:{sec:02d},{ms:03d}"

        # Write SRT with timestamps already scaled to the slowed duration
        with open(srt_path, "w", encoding="utf-8") as srt:
            t = 0.0
            for i, (text, dur) in enumerate(zip(texts, durations), 1):
                scaled_t   = t * slow_factor
                scaled_end = (t + dur - silence_pad) * slow_factor
                srt.write(f"{i}\n{fmt_srt(scaled_t)} --> {fmt_srt(scaled_end)}\n{text}\n\n")
                t += dur

        # ── Phase 3: Encode slide clips ───────────────────────────────────────
        preset = "ultrafast" if draft else "medium"
        clip_dir = os.path.join(work_dir, "clips")
        os.makedirs(clip_dir)
        progress(38, f"Encoding {len(images)} clips ({preset} preset)…")

        def make_clip(args):
            idx, img, wav = args
            out = os.path.join(clip_dir, f"clip_{idx:03d}.mp4")
            subprocess.run([
                FFMPEG, "-y",
                "-loop", "1", "-framerate", str(fps), "-i", img,
                "-i", wav,
                "-af", f"apad=pad_dur={silence_pad}",
                "-vf", f"scale={scale}:flags=lanczos,format=yuv420p",
                "-c:v", "libx264", "-tune", "stillimage", "-preset", preset,
                "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                out,
            ], check=True, capture_output=True)
            return idx, out

        clip_paths = [None] * len(images)
        with ThreadPoolExecutor(max_workers=max_clip_workers) as pool:
            futures = {pool.submit(make_clip, (i, img, wav)): i
                       for i, (img, wav) in enumerate(zip(images, wav_paths))}
            done = 0
            for future in as_completed(futures):
                idx, path = future.result()
                clip_paths[idx] = path
                done += 1
                progress(38 + int(done / len(images) * 50), f"Clip {done}/{len(images)} encoded")

        # ── Phase 4: Assemble final video ─────────────────────────────────────
        progress(89, "Assembling final video…")

        if draft:
            concat_file = os.path.join(work_dir, "concat.txt")
            with open(concat_file, "w") as f:
                for p in clip_paths:
                    f.write(f"file '{os.path.abspath(p)}'\n")
            subprocess.run([
                FFMPEG, "-y",
                "-f", "concat", "-safe", "0", "-i", concat_file,
                "-c:v", "copy",
                # Re-encode audio to fix non-monotonic DTS at clip boundaries
                "-c:a", "aac", "-b:a", "192k",
                output_path,
            ], check=True, capture_output=True)

        else:
            inputs = []
            for p in clip_paths:
                inputs += ["-i", p]

            n = len(clip_paths)
            offsets, t = [], 0.0
            for i in range(n - 1):
                t += durations[i] - fade_dur
                offsets.append(max(0.0, round(t, 3)))

            v_cur, a_cur = "[0:v]", "[0:a]"
            fc_parts = []
            for i in range(1, n):
                v_out = "[vout]" if i == n - 1 else f"[xv{i}]"
                a_out = "[aout]" if i == n - 1 else f"[xa{i}]"
                fc_parts.append(
                    f"{v_cur}[{i}:v]xfade=transition=fade"
                    f":duration={fade_dur}:offset={offsets[i-1]}{v_out}"
                )
                fc_parts.append(
                    f"{a_cur}[{i}:a]acrossfade=d={fade_dur}:o=1{a_out}"
                )
                v_cur, a_cur = v_out, a_out

            subprocess.run([
                FFMPEG, "-y",
            ] + inputs + [
                "-filter_complex", ";".join(fc_parts),
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                output_path,
            ], check=True, capture_output=True)

        # ── Phase 5 (optional): Slow down to target duration ──────────────────
        if slow_factor > 1.0:
            progress(92, f"Applying {slow_factor:.2f}x slowdown (pitch-preserved)…")
            atempo = 1.0 / slow_factor          # e.g. 0.82 for 1.22x slower
            tmp_slow = output_path + ".slow.mp4"

            # rubberband = pitch-preserving time-stretch (voices stay natural)
            # fallback to atempo if rubberband unavailable
            for audio_filter in [
                f"rubberband=tempo={atempo:.4f}",
                f"atempo={atempo:.4f}",
            ]:
                try:
                    subprocess.run([
                        FFMPEG, "-y", "-i", output_path,
                        "-vf", f"setpts={slow_factor:.4f}*PTS",
                        "-af", audio_filter,
                        "-c:v", "libx264", "-preset", preset, "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k",
                        tmp_slow,
                    ], check=True, capture_output=True)
                    break   # success — stop trying fallbacks
                except subprocess.CalledProcessError:
                    continue

            os.replace(tmp_slow, output_path)   # swap in-place

        progress(100, "Done!")
    # work_dir and everything inside (WAVs, clips) deleted here automatically
