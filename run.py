import os, re, time, json, asyncio, subprocess, requests, shutil, glob
from google import genai
from google.genai import types

# 1. CẤU HÌNH HỆ THỐNG CHO VIDEO DÀI
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
if not GEMINI_KEY:
    raise Exception("Chưa cài GEMINI_API_KEY trong GitHub Secrets!")

client = genai.Client(api_key=GEMINI_KEY)

CHANNEL_SUB_NAME = "lão hạc vietsub"

CONFIG = {
    "models": ["gemini-3.7-flash", "gemini-3.8-flash", "gemini-2.5-flash"],
    "voice": "EdgeTTS - Hoài My",
    "font_family": "Comfortaa",
    "font_size": 16,
    "default_y1": 0.89,
    "default_y2": 0.97
}

YAN_BASE_INSTRUCTIONS = """
Persona: YaN - Chuyên gia Biên dịch ngôn ngữ & Bản địa hóa phim hoạt hình Trung Quốc.
TỐI HẬU THƯ:
1. Mỗi entry phụ đề BẮT BUỘC chỉ được nằm trên DUY NHẤT 1 DÒNG ĐƠN (tối đa 10-12 từ).
2. Định dạng bắt buộc: 00:mm:ss,mmm --> 00:mm:ss,mmm (LUÔN CÓ ĐỦ 00: Ở CẢ 2 ĐẦU).
3. Tuyệt đối cách nhau bởi 1 dòng trống. CẤM DẤU NGOẶC KÉP ("). Không xuất lời dẫn.
"""

CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
if not os.path.exists(CJK_FONT):
    CJK_FONT = "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"

def get_video_dimensions(video_path: str):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", video_path]
    try:
        data = json.loads(subprocess.run(cmd, stdout=subprocess.PIPE, text=True).stdout)["streams"][0]
        return int(data["width"]), int(data["height"])
    except: return 1920, 1080

def get_audio_duration(file_path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path]
    try: return float(subprocess.run(cmd, stdout=subprocess.PIPE, text=True).stdout.strip())
    except: return 2.0

def clean_timestamp_str(t: str) -> str:
    t = t.strip().replace('.', ',')
    parts = t.split(':')
    if len(parts) == 2: return f"00:{int(parts[0]):02d}:{parts[1]}"
    elif len(parts) == 3: return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{parts[2]}"
    return t

def rebuild_pure_srt(raw_text: str) -> str:
    pattern = r'(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d{1,2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}|\d{1,2}:\d{2}[,\.]\d{1,3})'
    matches = list(re.finditer(pattern, raw_text))
    if not matches: return raw_text
    entries = []
    for i in range(len(matches)):
        st = clean_timestamp_str(matches[i].group(1))
        et = clean_timestamp_str(matches[i].group(2))
        p1 = matches[i].end()
        p2 = matches[i+1].start() if i + 1 < len(matches) else len(raw_text)
        lines = [l.strip() for l in raw_text[p1:p2].split('\n') if l.strip()]
        valid = [l for l in lines if not re.match(r'^\d+$', l) and '-->' not in l]
        txt = " ".join(valid).replace('"', '').strip()
        if txt: entries.append((st, et, txt))
    return "\n\n".join([f"{idx}\n{s} --> {e}\n{t}" for idx, (s, e, t) in enumerate(entries, 1)])

def parse_srt(srt_file: str):
    items = []
    if not os.path.exists(srt_file): return items
    with open(srt_file, "r", encoding="utf-8") as f: content = f.read()
    for m in re.finditer(r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\Z)', content, re.DOTALL):
        h1, mn1, s1 = m.group(2).split(':'); s1, ms1 = s1.split(',')
        h2, mn2, s2 = m.group(3).split(':'); s2, ms2 = s2.split(',')
        start = int(h1)*3600 + int(mn1)*60 + int(s1) + int(ms1)/1000.0
        end   = int(h2)*3600 + int(mn2)*60 + int(s2) + int(ms2)/1000.0
        items.append((start, end, m.group(4).strip().replace('\n', ' ')))
    return items

def get_srt_from_gemini_auto(video_path: str, model_list=CONFIG["models"], output_srt: str = "temp_sub.srt"):
    print("[*] Đang tải video lên bộ nhớ Gemini...")
    safe_video_path = "upload_for_gemini.mp4"
    shutil.copyfile(video_path, safe_video_path)
    v_file = client.files.upload(file=safe_video_path)
    while v_file.state.name == "PROCESSING":
        time.sleep(3)
        v_file = client.files.get(name=v_file.name)
    if v_file.state.name != "ACTIVE": raise Exception("Gemini không đọc được file video.")

    # Mở kịch trần 8192 tokens cho video dài 1 tiếng
    config = types.GenerateContentConfig(
        system_instruction=YAN_BASE_INSTRUCTIONS,
        temperature=0.1,
        max_output_tokens=8192
    )
    resp = None
    for model_name in model_list:
        print(f"[*] AI đang xem và dịch toàn bộ video bằng {model_name}...")
        for attempt in range(1, 4):
            try:
                r = client.models.generate_content(model=model_name, contents=[v_file, "Xuất SRT 1 dòng chuẩn theo quy tắc."], config=config)
                if r and r.text:
                    resp = r; print(f"[✓] Bóc sub thành công bằng {model_name}!"); break
            except Exception as e:
                print(f"[!] Thử lại model {model_name} lần {attempt} ({e})")
                time.sleep(3)
        if resp: break

    try: client.files.delete(name=v_file.name)
    except: pass

    if not resp or not resp.text: raise Exception("Không thể tạo phụ đề từ Gemini!")
    pure_srt = rebuild_pure_srt(resp.text)
    with open(output_srt, "w", encoding="utf-8") as f: f.write(pure_srt)

    first_time_match = re.search(r'00:(\d{2}):(\d{2}),(\d{3})', pure_srt)
    first_sec = 2.0
    if first_time_match:
        first_sec = int(first_time_match.group(1)) * 60 + int(first_time_match.group(2)) + 0.5
    return output_srt, first_sec

def detect_sub_zone_from_frame(video_path: str, timestamp_sec: float = 2.0, model_choice: str = "gemini-3.7-flash"):
    img_path = "sub_sample.jpg"
    subprocess.run(["ffmpeg", "-y", "-ss", str(timestamp_sec), "-i", video_path, "-vframes", "1", img_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not os.path.exists(img_path): return CONFIG["default_y1"], CONFIG["default_y2"]
    try:
        with open(img_path, "rb") as f: img_bytes = f.read()
        res = client.models.generate_content(
            model=model_choice,
            contents=[types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"), "Hãy nhìn vào dòng chữ phụ đề tiếng Trung chạy dưới đáy ảnh. Báo chính xác 2 số phần trăm: y_start, y_end tính từ đỉnh ảnh xuống."]
        ).text
        nums = re.findall(r'\d+', res)
        if len(nums) >= 2:
            y1, y2 = int(nums[0]) / 100.0, int(nums[1]) / 100.0
            y1, y2 = min(y1, y2), max(y1, y2)
            if 0.70 <= y1 < y2 <= 1.0: return y1, y2
    except: pass
    return CONFIG["default_y1"], CONFIG["default_y2"]

def generate_voice_file(text: str, voice_type: str, out_p: str) -> bool:
    import edge_tts
    v_code, pitch, rate = "vi-VN-HoaiMyNeural", "+16Hz", "+8%"
    if "Nam Minh" in voice_type: v_code, pitch, rate = "vi-VN-NamMinhNeural", "-2Hz", "+6%"
    try:
        asyncio.run(edge_tts.Communicate(text, v_code, pitch=pitch, rate=rate).save(out_p))
        return os.path.exists(out_p) and os.path.getsize(out_p) > 0
    except: return False

# XỬ LÝ ÂM THANH THEO CỤM (BATCHING) CHO HÀNG NGHÌN CÂU THOẠI
def build_synced_tts_track(srt_file: str, voice_type: str, output_audio: str = "full_tts.wav"):
    items = parse_srt(srt_file)
    os.makedirs("temp_tts", exist_ok=True)
    idx = 0
    audio_clips = []

    print(f"[*] Đang tạo giọng đọc cho {len(items)} câu thoại...")
    for start_sec, end_sec, text in items:
        if not text or text.startswith("["): continue
        raw_p = f"temp_tts/raw_{idx}.mp3"
        fit_p = f"temp_tts/fit_{idx}.mp3"
        try:
            if generate_voice_file(text, voice_type, raw_p):
                audio_dur = get_audio_duration(raw_p)
                max_allowed_dur = max(0.5, end_sec - start_sec - 0.08)
                if audio_dur > max_allowed_dur:
                    speed_factor = min(1.85, audio_dur / max_allowed_dur)
                    subprocess.run(["ffmpeg", "-y", "-i", raw_p, "-filter:a", f"atempo={speed_factor:.2f}", "-vn", fit_p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    chosen_p = fit_p
                else: chosen_p = raw_p

                audio_clips.append((chosen_p, int(start_sec * 1000)))
                idx += 1
        except: pass

    if not audio_clips: return None

    # Chia thành các cụm 35 câu để FFmpeg không bao giờ bị tràn giới hạn file Linux
    batch_size = 35
    batch_outputs = []
    print(f"[*] Đang trộn âm thanh thành { (len(audio_clips) // batch_size) + 1 } cụm...")

    for b_idx in range(0, len(audio_clips), batch_size):
        chunk = audio_clips[b_idx:b_idx+batch_size]
        inputs, parts = [], []
        for c_i, (c_path, c_delay) in enumerate(chunk):
            inputs.extend(["-i", c_path])
            parts.append(f"[{c_i}]adelay={c_delay}|{c_delay}[a{c_i}];")
        chain = "".join([f"[a{k}]" for k in range(len(chunk))])
        fc = "".join(parts) + f"{chain}amix=inputs={len(chunk)}:dropout_transition=0:normalize=0[out]"
        b_out = f"temp_tts/batch_{b_idx}.wav"
        subprocess.run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[out]", b_out], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(b_out):
            batch_outputs.append(b_out)

    # Trộn các cụm lại thành file audio hoàn chỉnh
    if len(batch_outputs) == 1:
        shutil.copyfile(batch_outputs[0], output_audio)
    else:
        inputs = []
        for b_p in batch_outputs: inputs.extend(["-i", b_p])
        chain = "".join([f"[{m}:a]" for m in range(len(batch_outputs))])
        fc = f"{chain}amix=inputs={len(batch_outputs)}:dropout_transition=0:normalize=0[out]"
        subprocess.run(["ffmpeg", "-y"] + inputs + ["-filter_complex", fc, "-map", "[out]", output_audio], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    return output_audio if os.path.exists(output_audio) else None

# RENDER SIÊU NHẸ CỐ ĐỊNH KHÔNG BỊ SẬP BỞI 1000 MỐC THỜI GIAN
def render_final_video_auto(input_video, srt_file, tts_audio, output_final, sub_name, y1_pct, y2_pct, sub_fontsize=16, font_family="Comfortaa"):
    w, h = get_video_dimensions(input_video)
    y_start = (int(h * y1_pct) // 2) * 2
    crop_h = (int(h * (y2_pct - y1_pct)) // 2) * 2
    crop_h = max(24, min(crop_h, h - y_start))

    y_center_pct = (y1_pct + y2_pct) / 2.0
    center_margin_v = max(4, int((1.0 - y_center_pct) * 288 - (int(sub_fontsize) / 4)))

    t_sub = sub_name.replace("'", "").replace(":", " -")
    srt_p = os.path.abspath(srt_file).replace("\\", "/")

    font_name = "Comfortaa"
    if "Roboto" in font_family: font_name = "Roboto"
    font_org_cmd = f":fontfile='{CJK_FONT}'" if os.path.exists(CJK_FONT) else ""

    # Dùng dải mờ cố định xuyên suốt phim: Vừa đẹp như bản gốc, vừa không làm sập FFmpeg
    vf = (
        f"[0:v]split[m][c];"
        f"[c]crop=iw:{crop_h}:0:{y_start},boxblur=12:1[b];"
        f"[m][b]overlay=0:{y_start}[v1];"
        f"[v1]drawtext=text='sub - {t_sub}':x=25:y=25{font_org_cmd}:fontcolor=white:shadowcolor=black:shadowx=1:shadowy=1,"
        f"drawtext=text='{t_sub}':x='mod(t*65,w+tw)-tw':y='mod(t*32,h+th)-th':fontsize=20:fontcolor=white@0.10,"
        f"subtitles=filename='{srt_p}':force_style='FontName={font_name},FontSize={int(sub_fontsize)},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.0,Alignment=2,MarginV={center_margin_v}'[vout]"
    )

    print("[*] Đang render video 1 tiếng với tốc độ cao...")
    if tts_audio and os.path.exists(tts_audio):
        af = "[0:a]volume=0.15[bg];[1:a]volume=1.0[voice];[bg][voice]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        cmd = ["ffmpeg", "-y", "-i", input_video, "-i", tts_audio, "-filter_complex", f"{vf};{af}", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", output_final]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_video, "-filter_complex", vf, "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-c:a", "copy", output_final]

    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        raise Exception(f"Lỗi FFmpeg: {res.stderr[-400:]}")
    return output_final

def clean_trash():
    shutil.rmtree("temp_tts", ignore_errors=True)
    for f in ["upload_for_gemini.mp4", "temp_sub.srt", "full_tts.wav", "sub_sample.jpg"]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass

def main():
    input_dir = "input_videos"
    output_dir = "output_videos"
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    raw_files = []
    for ext in ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.MP4", "*.MOV", "*.MKV"):
        raw_files.extend(glob.glob(os.path.join(input_dir, ext)))

    video_files = [f for f in raw_files if "_Vietsub" not in os.path.basename(f)]

    if not video_files:
        raise Exception("[!] Không tìm thấy video gốc nào trong thư mục input_videos!")

    print(f"🚀 Tìm thấy {len(video_files)} video. Bắt đầu xử lý video dài...")
    success_count = 0

    for idx, video_path in enumerate(video_files, 1):
        clean_trash()
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        out_name = os.path.join(output_dir, f"{base_name}_Vietsub.mp4")
        print(f"\n=======================================================")
        print(f"▶️ [{idx}/{len(video_files)}] Đang làm phim dài: {base_name}")
        print(f"=======================================================")

        try:
            srt_p, first_sec = get_srt_from_gemini_auto(video_path)
            y1, y2 = detect_sub_zone_from_frame(video_path, first_sec, CONFIG["models"][0])
            tts_p = build_synced_tts_track(srt_p, CONFIG["voice"])
            render_final_video_auto(video_path, srt_p, tts_p, out_name, CHANNEL_SUB_NAME, y1, y2)
            print(f"[✓] HOÀN TẤT BẢN DÀI: {out_name}")
            success_count += 1
        except Exception as e:
            import traceback
            print(f"[❌] Lỗi khi xử lý {base_name}: {e}")
            traceback.print_exc()
        clean_trash()

    if success_count == 0:
        raise Exception("Không render thành công video nào!")

    print("\n🎉 ĐÃ XỬ LÝ XONG TẤT CẢ VIDEO!")

if __name__ == "__main__":
    main()le='{CJK_FONT}'" if os.path.exists(CJK_FONT) else ""

    vf = (
        f"[0:v]split[m][c];"
        f"[c]crop=iw:{crop_h}:0:{y_start},gblur=sigma=15[b];"
        f"[m][b]overlay=0:{y_start}:enable='{en_str}'[v1];"
        f"[v1]drawtext=text='sub - {t_sub}':x=25:y=25{font_org_cmd}:fontcolor=white:shadowcolor=black:shadowx=1:shadowy=1,"
        f"drawtext=text='{t_sub}':x='mod(t*65,w+tw)-tw':y='mod(t*32,h+th)-th':fontsize=20:fontcolor=white@0.10,"
        f"subtitles=filename='{srt_p}':force_style='FontName={font_name},FontSize={int(sub_fontsize)},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.0,Alignment=2,MarginV={center_margin_v}'[vout]"
    )

    if tts_audio and os.path.exists(tts_audio):
        af = "[0:a]volume=0.15[bg];[1:a]volume=1.0[voice];[bg][voice]amix=inputs=2:duration=first:dropout_transition=0[aout]"
        cmd = ["ffmpeg", "-y", "-i", input_video, "-i", tts_audio, "-filter_complex", f"{vf};{af}", "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", output_final]
    else:
        cmd = ["ffmpeg", "-y", "-i", input_video, "-filter_complex", vf, "-map", "[vout]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-c:a", "copy", output_final]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_final

def clean_trash():
    shutil.rmtree("temp_tts", ignore_errors=True)
    for f in ["upload_for_gemini.mp4", "temp_sub.srt", "full_tts.wav", "sub_sample.jpg"]:
        if os.path.exists(f): 
            try: os.remove(f)
            except: pass

def main():
    input_dir = "input_videos"
    output_dir = "output_videos"
    os.makedirs(input_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    raw_files = []
    for ext in ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.MP4", "*.MOV", "*.MKV"):
        raw_files.extend(glob.glob(os.path.join(input_dir, ext)))

    video_files = [f for f in raw_files if "_Vietsub" not in os.path.basename(f)]

    if not video_files:
        print("[!] Không tìm thấy video gốc nào cần xử lý!")
        return

    print(f"🚀 Tìm thấy tổng cộng {len(video_files)} video. Bắt đầu xử lý...")

    for idx, video_path in enumerate(video_files, 1):
        clean_trash()
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        out_name = os.path.join(output_dir, f"{base_name}_Vietsub.mp4")
        print(f"\n=======================================================")
        print(f"▶️ [{idx}/{len(video_files)}] Đang làm video: {base_name}")
        print(f"=======================================================")

        try:
            srt_p, first_sec = get_srt_from_gemini_auto(video_path)
            y1, y2 = detect_sub_zone_from_frame(video_path, first_sec, CONFIG["models"][0])
            tts_p = build_synced_tts_track(srt_p, CONFIG["voice"])
            render_final_video_auto(video_path, srt_p, tts_p, out_name, CHANNEL_SUB_NAME, y1, y2)
            print(f"[✓] HOÀN TẤT: {out_name}")
        except Exception as e:
            print(f"[❌] Lỗi khi xử lý {base_name}: {e}")
            print("⏩ Nhảy sang video tiếp theo...")
        clean_trash()

    print("\n🎉 ĐÃ XỬ LÝ XONG TẤT CẢ VIDEO!")

if __name__ == "__main__":
    main()
