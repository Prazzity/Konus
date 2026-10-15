"""Small application-level bits that don't belong to a single widget."""

from PyQt5.QtWidgets import QProxyStyle, QStyle

# The hint card (konus.widgets.Hint) fades in, but Qt decides *when* to ask for a tooltip:
# the wake-up delay lives in the style, and the default is close to a second, which feels
# sluggish next to the animation.
WAKE_UP_MS = 320        # first hint over a control
FALL_ASLEEP_MS = 260    # the next one, while the pointer stays in the same area


class Style(QProxyStyle):
    """Fusion, with the tooltip timings turned down."""

    def styleHint(self, hint, option=None, widget=None, data=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return WAKE_UP_MS
        if hint == QStyle.SH_ToolTip_FallAsleepDelay:
            return FALL_ASLEEP_MS
        return super().styleHint(hint, option, widget, data)
