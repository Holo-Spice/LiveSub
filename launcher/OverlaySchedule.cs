namespace LiveSub.Launcher;

/// <summary>
/// Decides which subtitle line the floating overlay shows and for how long. One tick is 100 ms.
///
/// Live mode appends the subtitle file one utterance at a time, but the overlay draws one line
/// at a time. A single utterance can hold several lines, so their messages arrive within a few
/// milliseconds of each other and a plain "replace the text" update would flash the whole
/// utterance in an instant: the long line the speaker actually started with disappears into the
/// short phrase that came last. That is what the overlay used to do, and this class is what
/// stops it. Each line is shown for as long as it was spoken, never less than
/// <see cref="DefaultMinDwellTicks"/>, so the overlay keeps the speaker's pace instead of the
/// speed of the recognition.
///
/// The delay of a line is relative to the moment the utterance reached the overlay, not to the
/// host's absolute clock: <see cref="Push"/> is the zero point. Three moments make up a line:
///
/// * it becomes due when the speech reaches it (<c>ShowAtTick</c>), so a line that was spoken
///   late in a long utterance waits its turn instead of appearing together with the rest;
/// * it then stays while it is being read (<c>HoldTicks</c>, from its own media duration, with
///   <see cref="DefaultMinDwellTicks"/> as the floor), and the line after it cannot cut it short;
/// * the last line of an utterance is left on screen until <see cref="GraceTicks"/> after its
///   reading time, so a caption is never wiped the moment its time is up.
///
/// It is separated from the window so the ordering and timing can be tested without a window, a
/// screen or a subtitle task.
/// </summary>
public sealed class OverlaySchedule
{
    /// <summary>One line waiting to be shown, with the delay it should be shown at.</summary>
    private sealed record Line(string Text, long ShowAtTick, long HoldTicks, bool Keep);

    /// <summary>
    /// The shortest a line may stay on screen. The measured live run had lines with 0.16s of
    /// media time after them, which is below reading speed, so a line never gets less than this
    /// even when the speaker was faster than that.
    /// </summary>
    public const long DefaultMinDwellTicks = 12;

    /// <summary>
    /// How long the last line of an utterance stays after it has had its reading time, before
    /// the screen is cleared.
    /// </summary>
    public const long GraceTicks = 150;

    private readonly List<Line> _queue = [];
    private readonly long _minDwell;
    // _shown counts the lines handed over before the current batch, _drawn the ones of the
    // current batch. Keeping them apart keeps the total monotonic across batches.
    private long _shown;
    private long _drawn;
    private long _base;
    // -1 means "nothing has been drawn yet", so the reading-time test cannot fire before the
    // first line is on screen.
    private long _drawnAt = -1;
    private long _drawnHold;
    private long _clearAt;

    public OverlaySchedule(long minDwellTicks = DefaultMinDwellTicks)
    {
        _minDwell = Math.Max(1, minDwellTicks);
    }

    /// <summary>The line on screen right now, or an empty string when nothing is shown.</summary>
    public string Visible { get; private set; } = "";

    /// <summary>
    /// How many lines have been handed out since the overlay was created, which is the host's
    /// queue counter. It only ever grows, including for the lines whose audio was already in the
    /// past when their batch arrived: the host compares it between ticks to notice a new line, so
    /// a counter that went backwards would make it draw the wrong text.
    /// </summary>
    public long Shown => _shown + _drawn;

    /// <summary>True while lines are still waiting their turn.</summary>
    public bool Playing => _queue.Count > 0;

    /// <summary>
    /// True while the overlay is occupied: a line is being read, still waiting for its turn, or
    /// inside its grace period. The host shows "waiting for speech" only when this is false.
    /// The tick is the host's own clock, not the one <see cref="Push"/> was called with.
    /// </summary>
    public bool Busy(long tick) => Playing || Visible.Length > 0 && tick - _base < _clearAt;

    /// <summary>
    /// Accepts one utterance at <paramref name="tick"/>, which becomes the zero point for the
    /// delays of its lines. Each entry is the text, its start and end on the media timeline in
    /// milliseconds, and whether the line may stay on screen after the utterance ends.
    ///
    /// <paramref name="elapsedMs"/> is where the audio already is when this batch arrives: 0 when
    /// the batch is at the start of the utterance, and the utterance's own length when recognition
    /// finished only after the speech had ended, which is the normal live case. Lines the audio
    /// has passed are shown at once, in order and each for its own reading time, so the overlay
    /// catches up with the speaker by construction instead of freezing on the last line; the
    /// delay only pushes a line later when the batch really did arrive before its audio.
    ///
    /// The measured live run is why the pacing exists at all: an utterance of 13.9s was written as
    /// five lines in the same instant, and drawing them as they arrived left the first, longest
    /// line visible for a few milliseconds.
    /// </summary>
    public void Push(IReadOnlyList<(string Text, long StartMs, long EndMs, bool Keep)> lines, long tick, long elapsedMs = 0)
    {
        // A new utterance takes over the screen: whatever the previous one left behind, and its
        // pending clear, are gone. This runs before the queue is refilled.
        Visible = "";
        _queue.Clear();
        _shown += _drawn;
        _drawn = 0;
        _base = tick;
        _drawnAt = -1;
        _drawnHold = 0;
        _clearAt = 0;

        for (int index = 0; index < lines.Count; index++)
        {
            var (text, startMs, endMs, keep) = lines[index];
            if (string.IsNullOrWhiteSpace(text)) continue;
            long delay = Math.Max(0, Math.Max(0, startMs) - elapsedMs) / 100;
            long span = Math.Max(0, endMs - startMs);
            // How long the line is readable: its own media duration, but never less than the
            // floor, otherwise a very short phrase would be pushed off before it can be read.
            long hold = Math.Max(_minDwell, span / 100 + 2);
            // The final entry of an utterance is the one that waits for the next utterance.
            _queue.Add(new Line(text, delay, hold, keep && index == lines.Count - 1));
        }
    }

    /// <summary>
    /// Hands out the line that is due now and reports whether the screen should be cleared.
    /// </summary>
    public bool Advance(long tick)
    {
        long now = tick - _base;

        if (Visible.Length > 0 && _queue.Count == 0 && now >= _clearAt)
        {
            Visible = "";
            _drawnHold = 0;
            return true;
        }

        // A line that is still being read is not replaced, however long the recognition took.
        if (Visible.Length > 0 && _drawnHold > 0 && now - _drawnAt < _drawnHold) return false;

        while (_queue.Count > 0)
        {
            var line = _queue[0];
            // Every line waits for its own audio, the last one included: a batch that arrived
            // before its utterance is over must not show text for speech that has not happened.
            if (line.ShowAtTick > now) break;
            _queue.RemoveAt(0);
            Visible = line.Text;
            _drawn++;
            _drawnAt = now;
            // A Keep line is the end of the utterance: it is shown and left there until the next
            // utterance replaces it, so a pause never wipes a caption being read. Its grace
            // period is what eventually clears it if the speech really did stop.
            _drawnHold = line.Keep ? 0 : line.HoldTicks;
            _clearAt = now + line.HoldTicks + GraceTicks;
            break;
        }
        return false;
    }
}
