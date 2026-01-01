"""SRT (SubRip) format generation from Fireflies sentences."""

from datetime import timedelta


def ms_to_srt_timestamp(ms: int | float) -> str:
    """Convert milliseconds to SRT timestamp format (HH:MM:SS,mmm).

    Args:
        ms: Milliseconds (int or float)

    Returns:
        SRT formatted timestamp string
    """
    ms = int(ms)  # Ensure integer
    td = timedelta(milliseconds=ms)
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    milliseconds = ms % 1000

    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def sentences_to_srt(sentences: list[dict]) -> str:
    """Convert Fireflies sentences to SRT format.

    Args:
        sentences: List of sentence dicts with text, speaker_name, start_time, end_time

    Returns:
        SRT formatted string
    """
    if not sentences:
        return ""

    srt_lines = []

    for i, sentence in enumerate(sentences, start=1):
        text = sentence.get("text", "").strip()
        speaker = sentence.get("speaker_name", "Unknown")
        start_ms = sentence.get("start_time", 0) or 0
        end_ms = sentence.get("end_time", 0) or 0

        # Skip empty sentences
        if not text:
            continue

        start_ts = ms_to_srt_timestamp(start_ms)
        end_ts = ms_to_srt_timestamp(end_ms)

        # Format: sequence number, timestamp, speaker: text
        srt_lines.extend([
            str(i),
            f"{start_ts} --> {end_ts}",
            f"[{speaker}] {text}",
            "",  # Blank line between entries
        ])

    return "\n".join(srt_lines)


def sentences_to_txt(sentences: list[dict]) -> str:
    """Convert Fireflies sentences to plain text transcript.

    Groups consecutive sentences by speaker.

    Args:
        sentences: List of sentence dicts with text, speaker_name

    Returns:
        Plain text transcript with speaker labels
    """
    if not sentences:
        return ""

    lines = []
    current_speaker = None

    for sentence in sentences:
        text = sentence.get("text", "").strip()
        speaker = sentence.get("speaker_name", "Unknown")

        if not text:
            continue

        if speaker != current_speaker:
            if lines:
                lines.append("")  # Blank line between speakers
            lines.append(f"**{speaker}:**")
            current_speaker = speaker

        lines.append(text)

    return "\n".join(lines)
