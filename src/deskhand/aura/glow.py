"""The aura's glow: an orange band along the inside of a window's edges.

A thin line sits on the edge, and a band fades from it into the window over
about FADE points. The band is an inner shadow: a shape covering everything
outside the window's rounded rectangle casts a soft orange shadow, and a mask
cut to that rectangle keeps only the part inside the window. Drawing inside the
edge keeps all four sides visible when the window touches the screen edges. The
whole glow pulses slowly.
"""

# PyObjC builds its AppKit, Quartz and objc names at runtime and ships no type
# stubs, so pyright can't see them; the names are checked when the module runs.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Any

import Quartz

from deskhand.aura.screen import orange

EDGE = 2.0  # the solid line on the window's edge, in points
FADE = 28.0  # how far the glow fades into the window, in points
CORNER = 12.0  # close to a macOS window's corner radius
PULSE_S = 2.6  # seconds from dim to bright; a full pulse takes twice that


def _slow_pulse() -> Any:
    pulse = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
    pulse.setFromValue_(0.45)
    pulse.setToValue_(1.0)
    pulse.setDuration_(PULSE_S)
    pulse.setAutoreverses_(True)
    pulse.setRepeatCount_(float("inf"))
    pulse.setTimingFunction_(
        Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut)
    )
    return pulse


class Glow:
    def __init__(self, view: Any) -> None:
        self.root = Quartz.CALayer.layer()
        self.mask = Quartz.CAShapeLayer.layer()
        self.root.setMask_(self.mask)
        self.band = Quartz.CAShapeLayer.layer()
        self.band.setFillRule_(Quartz.kCAFillRuleEvenOdd)
        self.band.setFillColor_(orange())
        self.band.setShadowColor_(orange())
        self.band.setShadowOffset_((0, 0))
        self.band.setShadowRadius_(FADE / 2)
        self.band.setShadowOpacity_(1.0)
        self.edge = Quartz.CAShapeLayer.layer()
        self.edge.setFillColor_(None)
        self.edge.setStrokeColor_(orange())
        self.edge.setLineWidth_(EDGE * 2)  # the mask cuts away the outer half
        self.root.addSublayer_(self.band)
        self.root.addSublayer_(self.edge)
        self.root.addAnimation_forKey_(_slow_pulse(), "pulse")
        view.layer().addSublayer_(self.root)
        self.size: tuple[float, float] | None = None

    def layout(self, width: float, height: float) -> None:
        """Rebuild the shapes for a window of this size (only when the size changes)."""
        if self.size == (width, height):
            return
        self.size = (width, height)
        bounds = Quartz.CGRectMake(0, 0, width, height)
        radius = min(CORNER, width / 2, height / 2)
        window_shape = Quartz.CGPathCreateWithRoundedRect(bounds, radius, radius, None)
        outside = Quartz.CGPathCreateMutable()
        Quartz.CGPathAddRect(outside, None, Quartz.CGRectInset(bounds, -4 * FADE, -4 * FADE))
        Quartz.CGPathAddPath(outside, None, window_shape)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)  # follow resizes without lag
        for layer in (self.root, self.mask, self.band, self.edge):
            layer.setFrame_(bounds)
        self.mask.setPath_(window_shape)
        self.band.setPath_(outside)
        self.edge.setPath_(window_shape)
        Quartz.CATransaction.commit()
