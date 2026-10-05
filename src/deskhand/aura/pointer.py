"""deskhand's own cursor: where the agent points, on whichever display that is.

Cua Driver draws its agent cursor on the main display only and places it with
window coordinates as if they were screen coordinates, so it showed up on the
wrong screen. This is a small arrow in an overlay window of its own, moved to
the screen point the controller works out from the driver's snapshots. It glides
to each new point, ripples on clicks, sits above other windows like a real
pointer, and hides whenever the aura does.
"""

# PyObjC builds its AppKit, Quartz and objc names at runtime and ships no type
# stubs, so pyright can't see them; the names are checked when the module runs.
# pyright: reportAttributeAccessIssue=false

from __future__ import annotations

from typing import Any

import Quartz
from AppKit import NSAnimationContext, NSStatusWindowLevel
from Foundation import NSMakeRect

from deskhand.aura.screen import cocoa_y, make_overlay_window, orange

SIZE = 72.0  # the window around the tip, with room for the click ripple
GLIDE_S = 0.22  # seconds to glide to a new point
RIPPLE_S = 0.45
# A classic arrow pointer, tip at (0, 0), y pointing down, in points.
ARROW = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10.5, 18.8), (7.5, 12), (13, 12)]


def _arrow_path(cx: float, cy: float) -> Any:
    path = Quartz.CGPathCreateMutable()
    (x0, y0), *rest = ARROW
    Quartz.CGPathMoveToPoint(path, None, cx + x0, cy - y0)
    for x, y in rest:
        Quartz.CGPathAddLineToPoint(path, None, cx + x, cy - y)
    Quartz.CGPathCloseSubpath(path)
    return path


class Pointer:
    def __init__(self) -> None:
        self.window = make_overlay_window()
        self.window.setLevel_(NSStatusWindowLevel)
        self.window.setFrame_display_(NSMakeRect(0, 0, SIZE, SIZE), False)
        self.root = self.window.contentView().layer()
        center = SIZE / 2
        arrow = Quartz.CAShapeLayer.layer()
        arrow.setPath_(_arrow_path(center, center))
        arrow.setFillColor_(Quartz.CGColorCreateSRGB(1.0, 1.0, 1.0, 1.0))
        arrow.setStrokeColor_(orange())
        arrow.setLineWidth_(1.5)
        arrow.setLineJoin_(Quartz.kCALineJoinRound)
        arrow.setShadowOpacity_(0.35)
        arrow.setShadowRadius_(2.0)
        arrow.setShadowOffset_((0, -1))
        self.root.addSublayer_(arrow)
        self.placed = False

    def move(self, x: float, y: float, click: bool) -> None:
        """Put the tip on the screen point (x, y), given in Quartz coordinates.

        The glide animates the window's frame: through the animator,
        setFrame:display: moves the window, while setFrameOrigin: silently did
        nothing, which left the cursor behind on its first spot.
        """
        frame = NSMakeRect(x - SIZE / 2, cocoa_y(y) - SIZE / 2, SIZE, SIZE)
        if self.placed and self.window.isVisible():

            def glide(context: Any) -> None:
                context.setDuration_(GLIDE_S)
                self.window.animator().setFrame_display_(frame, True)

            NSAnimationContext.runAnimationGroup_completionHandler_(glide, None)
        else:
            self.window.setFrame_display_(frame, True)
        self.placed = True
        if click:
            self.ripple()

    def ripple(self) -> None:
        """An orange ring that grows out of the tip and fades."""
        ring = Quartz.CAShapeLayer.layer()
        radius = 14.0
        ring.setFrame_(((SIZE / 2 - radius, SIZE / 2 - radius), (2 * radius, 2 * radius)))
        ring.setPath_(
            Quartz.CGPathCreateWithEllipseInRect(((0, 0), (2 * radius, 2 * radius)), None)
        )
        ring.setFillColor_(None)
        ring.setStrokeColor_(orange())
        ring.setLineWidth_(2.0)
        ring.setOpacity_(0.0)
        grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
        grow.setFromValue_(0.3)
        grow.setToValue_(1.6)
        fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        fade.setFromValue_(0.9)
        fade.setToValue_(0.0)
        both = Quartz.CAAnimationGroup.animation()
        both.setAnimations_([grow, fade])
        both.setDuration_(RIPPLE_S)
        Quartz.CATransaction.begin()  # the completion block covers what's added below
        Quartz.CATransaction.setCompletionBlock_(ring.removeFromSuperlayer)
        self.root.addSublayer_(ring)
        ring.addAnimation_forKey_(both, "ripple")
        Quartz.CATransaction.commit()

    def show(self) -> None:
        if self.placed:
            self.window.orderFrontRegardless()

    def hide(self) -> None:
        self.window.orderOut_(None)
