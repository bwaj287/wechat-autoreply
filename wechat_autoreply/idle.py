from Quartz import (
    CGEventSourceSecondsSinceLastEventType,
    kCGEventFlagsChanged,
    kCGEventKeyDown,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseDragged,
    kCGEventMouseMoved,
    kCGEventOtherMouseDown,
    kCGEventOtherMouseDragged,
    kCGEventRightMouseDown,
    kCGEventRightMouseDragged,
    kCGEventScrollWheel,
    kCGAnyInputEventType,
    kCGEventSourceStateHIDSystemState,
)


def get_idle_time_seconds() -> float:
    # Treat both keyboard and mouse as user activity. The previous keyboard-only
    # heuristic could report "idle >= 30s" while the user was actively moving
    # the mouse, which made claim/send kick in too early.
    user_input_event_types = (
        kCGEventKeyDown,
        kCGEventFlagsChanged,
        kCGEventMouseMoved,
        kCGEventLeftMouseDown,
        kCGEventRightMouseDown,
        kCGEventOtherMouseDown,
        kCGEventLeftMouseDragged,
        kCGEventRightMouseDragged,
        kCGEventOtherMouseDragged,
        kCGEventScrollWheel,
    )
    samples: list[float] = []
    for event_type in user_input_event_types:
        seconds = float(
            CGEventSourceSecondsSinceLastEventType(
                kCGEventSourceStateHIDSystemState,
                event_type,
            )
        )
        if seconds >= 0:
            samples.append(seconds)
    if samples:
        return float(min(samples))
    return float(
        CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateHIDSystemState,
            kCGAnyInputEventType,
        )
    )
