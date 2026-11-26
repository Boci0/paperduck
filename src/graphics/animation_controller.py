"""Frame timing and animation state playback controller for Paperduck."""

from __future__ import annotations

from dataclasses import dataclass

from src.graphics.duck import ALL_ANIMATIONS, ONESHOT_ANIMATIONS, Pose, get_durations, get_poses


@dataclass(frozen=True)
class AnimationClip:
    """Immutable sequence of duck poses (any frame objects) paired with per-frame durations."""

    name: str
    frames: list[Pose]
    durations_ms: list[int]
    loop: bool = True

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError(f"AnimationClip {self.name!r} must contain at least one frame.")
        if len(self.frames) != len(self.durations_ms):
            raise ValueError(
                f"AnimationClip {self.name!r} frames ({len(self.frames)}) "
                f"must match durations_ms ({len(self.durations_ms)})."
            )
        if any(d <= 0 for d in self.durations_ms):
            raise ValueError(f"AnimationClip {self.name!r} durations must be positive ms.")


class AnimationController:
    """Manages active animation clips, frame timing, looping, and one-shot overlays."""

    def __init__(self, size: int = 32, initial_state: str = "idle") -> None:
        self._size = size
        self._clips: dict[str, AnimationClip] = {}
        self._load_default_clips(size)

        self._base_state: str = initial_state if initial_state in self._clips else "idle"
        self._oneshot_state: str | None = None
        self._frame_index: int = 0
        self._elapsed_in_frame_ms: int = 0
        self._facing_left: bool = False

    def _load_default_clips(self, size: int) -> None:
        for name in ALL_ANIMATIONS:
            self._clips[name] = AnimationClip(
                name=name, frames=get_poses(name), durations_ms=get_durations(name),
                loop=name not in ONESHOT_ANIMATIONS,
            )

    @property
    def size(self) -> int:
        return self._size

    @property
    def base_state(self) -> str:
        return self._base_state

    @property
    def active_clip_name(self) -> str:
        return self._oneshot_state if self._oneshot_state is not None else self._base_state

    @property
    def frame_index(self) -> int:
        return self._frame_index

    @property
    def facing_left(self) -> bool:
        return self._facing_left

    @facing_left.setter
    def facing_left(self, value: bool) -> None:
        self._facing_left = bool(value)

    def register_clip(self, clip: AnimationClip) -> None:
        """Register or replace a named animation clip."""
        self._clips[clip.name] = clip

    def set_state(self, state: str, *, force: bool = False) -> bool:
        """Set looping base animation state; returns True if playback reset."""
        if state not in self._clips:
            raise KeyError(f"Unknown animation clip: {state!r}")

        if not force and self._base_state == state and self._oneshot_state is None:
            return False

        self._base_state = state
        self._oneshot_state = None
        self._frame_index = 0
        self._elapsed_in_frame_ms = 0
        return True

    def play_oneshot(self, state: str) -> None:
        """Play a non-looping reaction clip once, then return to base_state."""
        if state not in self._clips:
            raise KeyError(f"Unknown animation clip: {state!r}")
        self._oneshot_state = state
        self._frame_index = 0
        self._elapsed_in_frame_ms = 0

    def current_clip(self) -> AnimationClip:
        return self._clips[self.active_clip_name]

    def current_frame_duration_ms(self) -> int:
        clip = self.current_clip()
        return clip.durations_ms[self._frame_index]

    def current_frame(self) -> Pose:
        """The pose to draw; the renderer mirrors it when `facing_left`."""
        return self.current_clip().frames[self._frame_index]

    def advance(self, elapsed_ms: int) -> bool:
        """Advance playback clock by elapsed_ms; returns True if visible frame changed."""
        if elapsed_ms <= 0:
            return False

        prev_clip = self.active_clip_name
        prev_index = self._frame_index
        self._elapsed_in_frame_ms += elapsed_ms

        clip = self.current_clip()
        if self._oneshot_state is None and clip.loop:
            total = sum(clip.durations_ms)
            if total > 0 and self._elapsed_in_frame_ms >= total:
                self._elapsed_in_frame_ms %= total

        while True:
            clip = self.current_clip()
            frame_duration = clip.durations_ms[self._frame_index]
            if self._elapsed_in_frame_ms < frame_duration:
                break

            self._elapsed_in_frame_ms -= frame_duration
            next_index = self._frame_index + 1

            if next_index < len(clip.frames):
                self._frame_index = next_index
            elif self._oneshot_state is not None:
                # Oneshot finished; revert cleanly to base_state
                self._oneshot_state = None
                self._frame_index = 0
                self._elapsed_in_frame_ms = 0
                break
            elif clip.loop:
                self._frame_index = 0
            else:
                self._frame_index = len(clip.frames) - 1
                self._elapsed_in_frame_ms = 0
                break

        return (self.active_clip_name != prev_clip) or (self._frame_index != prev_index)
