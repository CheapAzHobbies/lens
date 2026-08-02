#!/usr/bin/env python3
"""
Lens — a fast, mobile-first camera app for Linux tablets.
GTK4 + libadwaita + GStreamer.
"""

import gi, os, sys, datetime, pathlib
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Adw, Gst, GLib, Gdk, Gio, GObject, GdkPixbuf

Gst.init(None)

APP_ID = "org.cheapaz.Lens"
PICTURES = pathlib.Path.home() / "Pictures" / "Lens"
VIDEOS   = pathlib.Path.home() / "Videos"  / "Lens"
PICTURES.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)


def list_v4l2_cameras():
    """Return [(path, human_name)] for real video capture devices.

    A single physical camera often exposes multiple /dev/videoN nodes
    (capture, metadata, subdev). We keep only nodes that report
    "Video Capture" in their capabilities.
    """
    import subprocess
    devs = []
    seen_cards = set()
    for d in sorted(pathlib.Path("/dev").glob("video*")):
        try:
            info = subprocess.run(["v4l2-ctl", "-d", str(d), "--info"],
                                  capture_output=True, text=True, timeout=1).stdout
        except Exception:
            continue
        # Must be an actual capture device (not metadata/subdev)
        # Look at Device Caps line — should include "Video Capture"
        capture = False
        card = "Camera"
        for line in info.splitlines():
            line = line.strip()
            if line.startswith("Card type"):
                card = line.split(":", 1)[1].strip()
            if "Video Capture" in line and "Metadata" not in line:
                capture = True
        if not capture:
            continue
        if card in seen_cards:  # one node per physical camera
            continue
        seen_cards.add(card)
        devs.append((str(d), card))
    return devs


class LensWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Lens")
        # Landscape default — works well on laptops. Resizable to portrait for tablets.
        self.set_default_size(960, 640)
        self.set_size_request(320, 400)   # allow shrinking down to phone-ish sizes
        self.set_resizable(True)
        self.pipeline = None
        self.paintable = None
        self.cameras = list_v4l2_cameras() or [("/dev/video0", "Camera")]
        self.cam_idx = 0
        print(f"Lens: detected {len(self.cameras)} camera(s):")
        for c in self.cameras: print(f"   {c}")
        self.video_mode = False
        self.recording = False
        self.grid_visible = False
        self.timer_sec = 0
        self.aspect_idx = 0  # 0=4:3, 1=16:9, 2=1:1
        self.aspects = ["4:3", "16:9", "1:1"]
        self.last_photo = None
        self.countdown_val = 0

        self._build_ui()
        self._start_pipeline()

    # ---------- pipeline ----------
    def _start_pipeline(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
            self.pipeline = None
            self.paintable = None

        dev = self.cameras[self.cam_idx][0]
        # Preview branch + photo-capture branch, both fed by the same v4l2src via tee.
        # The photo branch produces encoded JPEG buffers on the appsink,
        # and we pull the latest one when the shutter is pressed.
        # Let the camera pick its native resolution — forcing 1280x720 stretched
        # 4:3 sensors (like the Z13 rear camera at 2592x1944) into 16:9.
        pipe_str = (
            f"v4l2src device={dev} ! videoconvert ! tee name=t "
            f"t. ! queue max-size-buffers=2 leaky=downstream ! gtk4paintablesink name=sink "
            f"t. ! queue max-size-buffers=2 leaky=downstream ! jpegenc quality=92 ! "
            f"appsink name=photosink emit-signals=false max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(pipe_str)
        sink = self.pipeline.get_by_name("sink")
        self.paintable = sink.props.paintable
        self.picture.set_paintable(self.paintable)

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_err)
        self.pipeline.set_state(Gst.State.PLAYING)

    def _on_bus_err(self, bus, msg):
        err, dbg = msg.parse_error()
        print("gst error:", err, dbg, file=sys.stderr)

    # ---------- UI ----------
    def _build_ui(self):
        overlay = Gtk.Overlay()
        self.set_content(overlay)

        # Black background + viewfinder
        bg = Gtk.Box()
        bg.set_hexpand(True); bg.set_vexpand(True)
        bg.add_css_class("bg-black")
        overlay.set_child(bg)

        self.picture = Gtk.Picture()
        self.picture.set_can_shrink(True)
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)  # centers image within widget
        self.picture.set_hexpand(True)
        self.picture.set_vexpand(True)
        # halign/valign default FILL so the Picture fills the window; CONTAIN
        # then centers the video inside it → equal bars on the shorter axis.
        bg.append(self.picture)

        # ---- Top control row ----
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_valign(Gtk.Align.START); top.set_halign(Gtk.Align.CENTER)
        top.set_margin_top(12)
        overlay.add_overlay(top)

        self.btn_grid   = self._pill_button("#",   self._toggle_grid,   width=42, tint=False)
        self.btn_aspect = self._pill_button(self.aspects[0], self._toggle_aspect, width=58, tint=False)
        self.btn_timer  = self._pill_button("⏱",  self._toggle_timer,  width=58, tint=False)
        top.append(self.btn_grid); top.append(self.btn_aspect); top.append(self.btn_timer)

        # Fullscreen toggle — top-right corner
        self.btn_fullscreen = self._pill_button("⛶", self._toggle_fullscreen, width=42, tint=False)
        self.btn_fullscreen.set_halign(Gtk.Align.END)
        self.btn_fullscreen.set_valign(Gtk.Align.START)
        self.btn_fullscreen.set_margin_top(12)
        self.btn_fullscreen.set_margin_end(12)
        overlay.add_overlay(self.btn_fullscreen)

        # Exit — top-left corner
        self.btn_exit = self._pill_button("✕", lambda: self.close(), width=42, tint=False)
        self.btn_exit.set_halign(Gtk.Align.START)
        self.btn_exit.set_valign(Gtk.Align.START)
        self.btn_exit.set_margin_top(12)
        self.btn_exit.set_margin_start(12)
        overlay.add_overlay(self.btn_exit)

        # ---- Bottom control area ----
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        bottom.set_valign(Gtk.Align.END)
        bottom.add_css_class("bottom-bar")
        # Natural min via child widgets, don't force size — lets window shrink freely
        overlay.add_overlay(bottom)

        # Mode pill
        mode_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        mode_row.set_halign(Gtk.Align.CENTER)
        mode_row.set_margin_top(12); mode_row.set_margin_bottom(6)
        mode_row.add_css_class("mode-pill")
        self.btn_photo = Gtk.Button(label="PHOTO")
        self.btn_photo.add_css_class("mode-btn"); self.btn_photo.add_css_class("mode-active")
        self.btn_photo.connect("clicked", lambda *_: self._set_video_mode(False))
        self.btn_video = Gtk.Button(label="VIDEO")
        self.btn_video.add_css_class("mode-btn")
        self.btn_video.connect("clicked", lambda *_: self._set_video_mode(True))
        mode_row.append(self.btn_photo); mode_row.append(self.btn_video)
        bottom.append(mode_row)

        # Action area — Gtk.Overlay so each button is positioned independently.
        # Shutter stays dead-center regardless of deck/flip sizes.
        act_area = Gtk.Overlay()
        act_area.set_size_request(-1, 160)   # tall enough for the enlarged deck
        act_area.set_margin_top(12); act_area.set_hexpand(True)
        act_base = Gtk.Box(); act_base.set_hexpand(True); act_base.set_vexpand(True)
        act_area.set_child(act_base)
        bottom.append(act_area)

        # SHUTTER — always centered in the action area
        self.shutter = Gtk.Button()
        self.shutter.set_size_request(80, 80); self.shutter.add_css_class("shutter")
        self.shutter.set_halign(Gtk.Align.CENTER); self.shutter.set_valign(Gtk.Align.CENTER)
        self.shutter.set_hexpand(False); self.shutter.set_vexpand(False)
        self.shutter.connect("clicked", lambda *_: self._on_shutter())
        act_area.add_overlay(self.shutter)

        # Deck thumbnail — anchored LEFT, does not affect shutter position
        self.thumb = ThumbnailDeck(
            on_click=self._open_gallery,
            on_card_click=self._open_photo_viewer,
        )
        act_area.add_overlay(self.thumb)
        self._refresh_deck()

        # Flip camera — anchored RIGHT
        self.btn_flip = Gtk.Button(label="⟳")
        self.btn_flip.set_size_request(56, 56); self.btn_flip.add_css_class("flip")
        self.btn_flip.set_halign(Gtk.Align.END); self.btn_flip.set_valign(Gtk.Align.CENTER)
        self.btn_flip.set_hexpand(False); self.btn_flip.set_vexpand(False)
        self.btn_flip.set_margin_end(28)
        self.btn_flip.set_sensitive(len(self.cameras) > 1)
        self.btn_flip.connect("clicked", lambda *_: self._flip_camera())
        act_area.add_overlay(self.btn_flip)

        # Countdown overlay
        self.countdown_label = Gtk.Label(label="")
        self.countdown_label.set_visible(False)
        self.countdown_label.add_css_class("countdown")
        self.countdown_label.set_halign(Gtk.Align.CENTER)
        self.countdown_label.set_valign(Gtk.Align.CENTER)
        overlay.add_overlay(self.countdown_label)

        # Full-window white flash (photo capture feedback)
        self.flash_overlay = Gtk.Box()
        self.flash_overlay.add_css_class("flash")
        self.flash_overlay.set_visible(False)
        self.flash_overlay.set_can_target(False)
        overlay.add_overlay(self.flash_overlay)

        # Recording indicator — red dot + elapsed time in top-right of viewfinder
        rec_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rec_row.set_halign(Gtk.Align.END); rec_row.set_valign(Gtk.Align.START)
        rec_row.set_margin_top(60); rec_row.set_margin_end(16)
        self.rec_dot = Gtk.Box()
        self.rec_dot.add_css_class("rec-dot")
        self.rec_dot.set_size_request(14, 14)
        self.rec_time_label = Gtk.Label(label="00:00")
        self.rec_time_label.add_css_class("rec-time")
        rec_row.append(self.rec_dot); rec_row.append(self.rec_time_label)
        self.rec_indicator = rec_row
        self.rec_indicator.set_visible(False)
        overlay.add_overlay(self.rec_indicator)

        # Grid overlay (rule of thirds)
        self.grid_widget = _GridOverlay()
        self.grid_widget.set_visible(False)
        overlay.add_overlay(self.grid_widget)

        # Full-screen photo viewer (last overlay so it sits on top of everything)
        self.viewer = Gtk.Overlay()
        self.viewer.add_css_class("viewer")
        self.viewer.set_visible(False)
        viewer_bg = Gtk.Box(); viewer_bg.add_css_class("viewer-bg")
        viewer_bg.set_hexpand(True); viewer_bg.set_vexpand(True)
        self.viewer.set_child(viewer_bg)
        self.viewer_picture = Gtk.Picture()
        self.viewer_picture.set_can_shrink(True)
        self.viewer_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.viewer_picture.set_hexpand(True); self.viewer_picture.set_vexpand(True)
        viewer_bg.append(self.viewer_picture)
        # Back button — top-LEFT so it's clearly "back to camera" not "close app"
        # (the ✕ in the corner already means close-app)
        viewer_close = Gtk.Button(label="← Back")
        viewer_close.add_css_class("pill")
        viewer_close.set_size_request(-1, 42)
        viewer_close.set_halign(Gtk.Align.START); viewer_close.set_valign(Gtk.Align.START)
        viewer_close.set_margin_top(12); viewer_close.set_margin_start(12)
        viewer_close.connect("clicked", lambda *_: self._close_photo_viewer())
        self.viewer.add_overlay(viewer_close)
        # Also close on click anywhere in the viewer backdrop
        viewer_click = Gtk.GestureClick()
        viewer_click.connect("released", lambda *_: self._close_photo_viewer())
        viewer_bg.add_controller(viewer_click)
        overlay.add_overlay(self.viewer)

        # CSS
        css = Gtk.CssProvider()
        css.load_from_string("""
        .bg-black { background: black; }
        .bottom-bar { background: rgba(0,0,0,0.8); }
        .mode-pill { background: rgba(255,255,255,0.15); border-radius: 16px; padding: 2px; }
        .mode-btn { background: transparent; color: white; font-weight: bold;
                    font-size: 12px; padding: 4px 24px; border-radius: 14px;
                    border: none; box-shadow: none; }
        .mode-active { background: rgba(0,0,0,0.5); color: #ffcc00; }
        .shutter { background: white; border-radius: 40px;
                   border: 4px solid white; min-width: 76px; min-height: 76px;
                   box-shadow: 0 0 0 4px rgba(0,0,0,0.4);
                   transition: transform 80ms ease-out, background 100ms; }
        .shutter:active { background: #ddd; transform: scale(0.88); }
        .shutter:hover  { background: #f5f5f5; }
        .thumb          { min-width: 56px; min-height: 56px; padding: 0; }
        .thumb picture  { min-width: 56px; min-height: 56px; }
        .thumb:active   { transform: scale(0.9); }
        /* --- deck-of-cards thumbnail --- */
        .thumb-placeholder { color: rgba(255,255,255,0.6); }
        .deck-card {
            border-radius: 22px;                 /* much rounder corners */
            border: 3px solid white;
            box-shadow: 0 3px 8px rgba(0,0,0,0.55);
            transition: transform 320ms cubic-bezier(.2,.9,.3,1.2),
                        opacity   250ms ease-out;
        }
        /* Hand-of-cards fan — all rotate around a common bottom pivot.
           Draw order: idx-0 is deepest (leftmost), idx-4 is on top (rightmost),
           matching a hand of playing cards held together at the bottom. */
        .deck-card { transform-origin: 50% 100%; }   /* pivot at bottom-center */
        .state-idle.deck-idx-0 { transform: translate(80px, 8px) rotate(-30deg); }
        .state-idle.deck-idx-1 { transform: translate(80px, 8px) rotate(-15deg); }
        .state-idle.deck-idx-2 { transform: translate(80px, 8px) rotate(  0deg); }
        .state-idle.deck-idx-3 { transform: translate(80px, 8px) rotate( 15deg); }
        .state-idle.deck-idx-4 { transform: translate(80px, 8px) rotate( 30deg); }
        /* Expanded: fan them out — anchored middle-left, spread across the deck */
        .state-expanded.deck-idx-0 { transform: translate( 20px, -10px) rotate(-14deg) scale(1.05); }
        .state-expanded.deck-idx-1 { transform: translate( 65px, -35px) rotate( -7deg) scale(1.08); }
        .state-expanded.deck-idx-2 { transform: translate(110px, -45px) rotate(  0deg) scale(1.10); }
        .state-expanded.deck-idx-3 { transform: translate(150px, -35px) rotate(  7deg) scale(1.08); }
        .state-expanded.deck-idx-4 { transform: translate(190px, -10px) rotate( 14deg) scale(1.05); }
        /* "Pulled" — one card lifted farther and up */
        .pulled { transform: translate(0px, -80px) rotate(0deg) scale(1.4);
                  box-shadow: 0 8px 20px rgba(0,0,0,0.6); }
        /* Focused card (mouse over a specific card while deck is fanned) */
        .card-focused { transform: translate(100px, -95px) rotate(0deg) scale(1.5) !important;
                        box-shadow: 0 12px 30px rgba(0,0,0,0.7);
                        border: 2px solid white; }
        /* Full-screen photo viewer */
        .viewer-bg { background: rgba(0,0,0,0.95); }
        .flip:active    { transform: scale(0.9); }
        .pill:active    { transform: scale(0.9); }
        .rec-shutter { background: red; border-radius: 8px;
                       min-width: 40px; min-height: 40px; border: 4px solid white; }
        .thumb { background: rgba(255,255,255,0.2); border-radius: 8px;
                 border: 1px solid white; }
        .flip { background: rgba(0,0,0,0.5); border-radius: 28px;
                color: white; font-size: 22px; font-weight: bold; border: none; }
        .flip:disabled { opacity: 0.35; }
        .pill { background: rgba(0,0,0,0.5); border-radius: 21px; color: white;
                font-weight: bold; font-size: 14px; padding: 4px 12px; border: none; }
        .pill-active { background: #ffcc00; color: black; }
        .countdown { color: white; font-size: 96px; font-weight: bold;
                     background: rgba(0,0,0,0.5); padding: 30px 50px; border-radius: 60px; }
        .flash { background: white; }
        .rec-dot { background: #ff2222; border-radius: 7px; }
        .rec-dot.blink { background: rgba(255,34,34,0.3); }
        .rec-time { color: white; font-family: monospace; font-size: 14px;
                    font-weight: bold; background: rgba(0,0,0,0.5);
                    padding: 2px 8px; border-radius: 8px; }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Keyboard shortcuts
        ctrl = Gtk.ShortcutController()
        def _esc(*_):
            if self.viewer.get_visible():
                self._close_photo_viewer(); return
            self.unfullscreen()
        for accel, fn in [("space", self._on_shutter), ("Return", self._on_shutter),
                          ("f", self._flip_camera), ("g", self._toggle_grid),
                          ("F11", self._toggle_fullscreen),
                          ("Escape", _esc)]:
            sc = Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(accel),
                Gtk.CallbackAction.new(lambda w, args, f=fn: (f(), True)[1]))
            ctrl.add_shortcut(sc)
        self.add_controller(ctrl)

    def _pill_button(self, label, cb, width=42, tint=False):
        b = Gtk.Button(label=label)
        b.set_size_request(width, 42)
        b.add_css_class("pill")
        if tint: b.add_css_class("pill-active")
        b.connect("clicked", lambda *_: cb())
        return b

    # ---------- Actions ----------
    def _toggle_grid(self):
        self.grid_visible = not self.grid_visible
        self.grid_widget.set_visible(self.grid_visible)
        if self.grid_visible: self.btn_grid.add_css_class("pill-active")
        else: self.btn_grid.remove_css_class("pill-active")

    def _toggle_aspect(self):
        self.aspect_idx = (self.aspect_idx + 1) % len(self.aspects)
        self.btn_aspect.set_label(self.aspects[self.aspect_idx])
        # (Aspect ratio applied at capture; viewfinder is CONTAIN)

    def _toggle_timer(self):
        seq = [0, 3, 10]
        cur = seq.index(self.timer_sec) if self.timer_sec in seq else 0
        self.timer_sec = seq[(cur + 1) % len(seq)]
        if self.timer_sec > 0:
            self.btn_timer.set_label(f"{self.timer_sec}s")
            self.btn_timer.add_css_class("pill-active")
        else:
            self.btn_timer.set_label("⏱")
            self.btn_timer.remove_css_class("pill-active")

    def _set_video_mode(self, video):
        if self.recording: return
        self.video_mode = video
        if video:
            self.btn_video.add_css_class("mode-active")
            self.btn_photo.remove_css_class("mode-active")
        else:
            self.btn_photo.add_css_class("mode-active")
            self.btn_video.remove_css_class("mode-active")

    def _toggle_fullscreen(self):
        if self.is_fullscreen(): self.unfullscreen()
        else: self.fullscreen()

    def _flip_camera(self):
        if len(self.cameras) < 2: return
        self.cam_idx = (self.cam_idx + 1) % len(self.cameras)
        print(f"Lens: switching to camera {self.cam_idx}: {self.cameras[self.cam_idx]}")
        # Detach paintable before tearing down pipeline
        self.picture.set_paintable(None)
        self._start_pipeline()

    def _on_shutter(self):
        if self.video_mode:
            if self.recording: self._stop_recording()
            else: self._start_recording()
            return
        if self.timer_sec > 0:
            self.countdown_val = self.timer_sec
            self.countdown_label.set_visible(True)
            self.countdown_label.set_label(str(self.countdown_val))
            GLib.timeout_add_seconds(1, self._tick_countdown)
        else:
            self._take_photo()

    def _tick_countdown(self):
        self.countdown_val -= 1
        if self.countdown_val <= 0:
            self.countdown_label.set_visible(False)
            self._take_photo()
            return False
        self.countdown_label.set_label(str(self.countdown_val))
        return True

    def _take_photo(self):
        appsink = self.pipeline.get_by_name("photosink")
        if not appsink:
            print("Lens: no photosink in pipeline"); return

        # Try up to 6 times over ~1.5 sec — first buffer can be tiny warmup data.
        # NOTE: try_pull_sample is a GObject signal in Python, not a direct method.
        data = None
        for attempt in range(6):
            sample = appsink.emit("try-pull-sample", int(0.25 * Gst.SECOND))
            if not sample:
                print(f"Lens: attempt {attempt+1}: no sample"); continue
            buf = sample.get_buffer()
            size = buf.get_size()
            ok, mapinfo = buf.map(Gst.MapFlags.READ)
            if not ok: continue
            try:
                blob = bytes(mapinfo.data)  # copy out before unmap
            finally:
                buf.unmap(mapinfo)
            # A valid JPEG is at least a few KB and starts with 0xFF 0xD8
            if len(blob) > 1000 and blob[:2] == b"\xff\xd8":
                data = blob; break
            print(f"Lens: attempt {attempt+1}: got {size} bytes (not valid JPEG yet)")

        if not data:
            print("Lens: capture failed — no valid JPEG from pipeline")
            return

        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = PICTURES / f"Lens-{ts}.jpg"
        path.write_bytes(data)
        print(f"Lens: saved {path} ({path.stat().st_size // 1024} KB)")
        self.last_photo = str(path)
        # Flash animation + thumbnail update
        self._flash()
        self._refresh_deck()

    def _flash(self):
        """Full-screen white flash overlay ~150ms — visual capture feedback."""
        self.flash_overlay.set_visible(True)
        self.flash_overlay.set_opacity(0.9)
        # Fade out over 200ms
        def fade():
            o = self.flash_overlay.get_opacity() - 0.15
            if o <= 0:
                self.flash_overlay.set_visible(False)
                return False
            self.flash_overlay.set_opacity(o)
            return True
        GLib.timeout_add(20, fade)

    def _rec_tick(self):
        if not self.recording: return False
        self.rec_seconds += 1
        m, s = divmod(self.rec_seconds, 60)
        self.rec_time_label.set_label(f"{m:02d}:{s:02d}")
        # Blink dot every tick
        if self.rec_dot.has_css_class("blink"): self.rec_dot.remove_css_class("blink")
        else: self.rec_dot.add_css_class("blink")
        return True

    def _start_recording(self):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = VIDEOS / f"Lens-{ts}.mkv"
        # Rebuild pipeline for recording
        self.pipeline.set_state(Gst.State.NULL)
        dev = self.cameras[self.cam_idx][0]
        self.pipeline = Gst.parse_launch(
            f"v4l2src device={dev} ! tee name=t "
            f"t. ! queue ! videoconvert ! gtk4paintablesink name=sink "
            f"t. ! queue ! videoconvert ! x264enc tune=zerolatency bitrate=4000 ! "
            f"matroskamux ! filesink location={path}"
        )
        sink = self.pipeline.get_by_name("sink")
        self.paintable = sink.props.paintable
        self.picture.set_paintable(self.paintable)
        self.pipeline.set_state(Gst.State.PLAYING)
        self.recording = True
        self.shutter.add_css_class("rec-shutter")
        self.rec_seconds = 0
        self.rec_time_label.set_label("00:00")
        self.rec_indicator.set_visible(True)
        GLib.timeout_add_seconds(1, self._rec_tick)

    def _stop_recording(self):
        # Send EOS then tear down and restart preview
        self.pipeline.send_event(Gst.Event.new_eos())
        bus = self.pipeline.get_bus()
        bus.timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS)
        self.pipeline.set_state(Gst.State.NULL)
        self.recording = False
        self.shutter.remove_css_class("rec-shutter")
        self.rec_indicator.set_visible(False)
        self._start_pipeline()
        print(f"Lens: saved video ({self.rec_seconds}s)")

    def _open_gallery(self, *_):
        Gio.AppInfo.launch_default_for_uri("file://" + str(PICTURES), None)

    def _open_photo_viewer(self, path):
        """Show a photo full-screen inside Lens (not the whole OS window)."""
        try:
            self.viewer_picture.set_filename(path)
        except Exception as e:
            print("viewer load:", e); return
        self.viewer.set_visible(True)

    def _close_photo_viewer(self):
        self.viewer.set_visible(False)

    def _refresh_deck(self):
        """Feed the last N photos on disk into the thumbnail deck."""
        jpgs = sorted(PICTURES.glob("*.jpg"), key=lambda p: p.stat().st_mtime)
        self.thumb.update_photos(jpgs)


class ThumbnailDeck(Gtk.Overlay):
    """A stacked "deck of cards" thumbnail preview.

    Idle: shows just the top card (last photo).
    Hover: after 500ms delay, fans the deck out and starts a card-pull cycle
      where the bottom card animates up-and-out, holds briefly, then returns
      under the deck. Cycle continues until mouse leaves.
    """
    DECK_SIZE = 5     # up to N cards visible
    THUMB_PX  = 112   # collapsed edge (was 56 — user asked for 2x)
    # Bigger widget than the visible card so the fan-out region is inside
    # the hit area (GTK4 hit boxes don't follow CSS transforms).
    HIT_W     = 320
    HIT_H     = 160

    def __init__(self, on_click=None, on_card_click=None):
        super().__init__()
        self.set_size_request(self.HIT_W, self.HIT_H)
        self.set_hexpand(False); self.set_vexpand(False)
        # Centered vertically in the row, with breathing room from the left edge.
        self.set_valign(Gtk.Align.CENTER); self.set_halign(Gtk.Align.START)
        self.set_margin_start(28)
        self.on_click = on_click
        self.on_card_click = on_card_click
        self.card_paths = []
        self.cards = []        # bottom-to-top order
        self.hover_delay_id = None
        self.cycle_id = None
        self.expanded = False
        self.current_pull = 0

        # Placeholder icon when no photos yet
        self.placeholder = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
        self.placeholder.set_pixel_size(28)
        self.placeholder.add_css_class("thumb-placeholder")
        self.set_child(self.placeholder)

        # New interaction: press-and-hold to fan out, release on a card to
        # open it. Short click opens the most recent. Double click opens the
        # gallery folder in the file manager.
        self._hold_timer = None
        self._click_timer = None
        self._press_is_hold = False

        press = Gtk.GestureClick.new()
        press.set_button(1)   # left mouse
        press.connect("pressed",  self._on_press)
        press.connect("released", self._on_release)
        self.add_controller(press)

        # Motion controller — only used while in hold mode
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", lambda _c, x, y: self._on_motion(x, y))
        motion.connect("leave", lambda *_: self._on_leave())
        self.add_controller(motion)

    def _load_thumb(self, path, size=112):
        try:
            full = GdkPixbuf.Pixbuf.new_from_file(str(path))
        except Exception:
            return None
        side = min(full.get_width(), full.get_height())
        x = (full.get_width()  - side) // 2
        y = (full.get_height() - side) // 2
        sq = full.new_subpixbuf(x, y, side, side)
        scaled = sq.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        return Gdk.Texture.new_for_pixbuf(scaled)

    def update_photos(self, paths):
        """Rebuild the deck from these paths (oldest → newest at top)."""
        # Clear existing
        while self.cards:
            self.remove_overlay(self.cards.pop())
        self.set_child(None)
        latest = list(paths)[-self.DECK_SIZE:]
        self.card_paths = [str(p) for p in latest]
        if not latest:
            self.set_child(self.placeholder); return
        for i, p in enumerate(latest):
            tex = self._load_thumb(p, self.THUMB_PX * 2)
            if not tex: continue
            img = Gtk.Image.new_from_paintable(tex)
            img.set_pixel_size(self.THUMB_PX)
            img.add_css_class("deck-card")
            img.add_css_class(f"deck-idx-{i}")   # for stacking transforms
            # Clip the paintable to the widget's rounded corners so the image
            # doesn't bleed past the border.
            img.set_overflow(Gtk.Overflow.HIDDEN)

            # Anchor each card to the LEFT and vertically centered — this
            # keeps the thumbnail well inside the action area (avoids the
            # bottom-cutoff when the deck was anchored to END).
            img.set_halign(Gtk.Align.START); img.set_valign(Gtk.Align.CENTER)
            self.cards.append(img)
            self.add_overlay(img)
        self._focused_card = None
        self._refresh_transforms()

    def _refresh_transforms(self):
        """Apply CSS classes based on state so cards fan when expanded."""
        state = "expanded" if self.expanded else "idle"
        for i, c in enumerate(self.cards):
            for cls in ("state-idle", "state-expanded", "pulled"):
                if c.has_css_class(cls): c.remove_css_class(cls)
            c.add_css_class(f"state-{state}")

    def _on_motion(self, x, y):
        """Cursor moved inside the deck widget — pick focused card by x zone."""
        if not self.expanded or len(self.cards) < 2:
            return
        # Divide the widget width into N zones, one per card
        w = self.get_width() or self.HIT_W
        idx = int((x / max(w, 1)) * len(self.cards))
        idx = max(0, min(len(self.cards) - 1, idx))
        target = self.cards[idx]
        if self._focused_card is target:
            return
        for c in self.cards:
            if c.has_css_class("card-focused"):
                c.remove_css_class("card-focused")
        target.add_css_class("card-focused")
        self._focused_card = target

    # ---- new press/hold interaction ----
    HOLD_MS   = 220   # ms to hold before fan-out starts
    DBLCLK_MS = 280   # ms to wait for a possible second click before opening

    def _on_press(self, gesture, n_press, x, y):
        # If a single-click was pending from an earlier release, cancel it —
        # a second press means the user is going for a double-click OR a hold.
        if self._click_timer:
            GLib.source_remove(self._click_timer); self._click_timer = None
        # Start (or restart) hold timer
        if self._hold_timer:
            GLib.source_remove(self._hold_timer); self._hold_timer = None
        self._press_is_hold = False
        def _trigger():
            self._hold_timer = None
            self._press_is_hold = True
            self._expand()
            return False
        self._hold_timer = GLib.timeout_add(self.HOLD_MS, _trigger)

    def _on_release(self, gesture, n_press, x, y):
        # If we're mid-hold-timer, release before threshold = a click
        if self._hold_timer:
            GLib.source_remove(self._hold_timer); self._hold_timer = None
            if n_press >= 2:
                # Second click confirmed — open gallery
                if self.on_click: self.on_click()
            else:
                # First (short) click — defer to see if a second click follows
                def _fire_single():
                    self._click_timer = None
                    if self.card_paths and self.on_card_click:
                        self.on_card_click(self.card_paths[-1])
                    return False
                self._click_timer = GLib.timeout_add(self.DBLCLK_MS, _fire_single)
            return
        # Otherwise we were in hold/fan mode — open the focused card
        if self._press_is_hold:
            if self._focused_card and self._focused_card in self.cards:
                idx = self.cards.index(self._focused_card)
                if idx < len(self.card_paths) and self.on_card_click:
                    self.on_card_click(self.card_paths[idx])
            self._press_is_hold = False
            self._collapse()

    def _collapse(self):
        """Reverse of _expand: hide the fan, stop the cycle."""
        if self.cycle_id:
            GLib.source_remove(self.cycle_id); self.cycle_id = None
        self.expanded = False
        for c in self.cards:
            if c.has_css_class("card-focused"): c.remove_css_class("card-focused")
            if c.has_css_class("pulled"): c.remove_css_class("pulled")
        self._focused_card = None
        self._refresh_transforms()

    def _on_enter(self):
        # Hover no longer triggers the fan — kept for API stability but unused.
        pass

    def _on_leave(self):
        # If the mouse leaves the deck mid-hold, cancel the fan.
        if self._hold_timer:
            GLib.source_remove(self._hold_timer); self._hold_timer = None
        if self.expanded:
            self._collapse()

    def _expand(self):
        self.hover_delay_id = None
        if not self.cards: return False
        self.expanded = True
        self._refresh_transforms()
        self.current_pull = 0
        self.cycle_id = GLib.timeout_add(1200, self._cycle_pull)
        return False

    def _cycle_pull(self):
        if not self.expanded or not self.cards: return False
        # If user is focused on a specific card, don't disturb it
        if getattr(self, "_focused_card", None):
            return True
        # Un-pull previous card
        for c in self.cards:
            if c.has_css_class("pulled"): c.remove_css_class("pulled")
        # Pull next card (skip the top-most, i == len-1, since it's already visible)
        if len(self.cards) > 1:
            idx = self.current_pull % (len(self.cards) - 1)
            self.cards[idx].add_css_class("pulled")
            self.current_pull += 1
        return True


class _GridOverlay(Gtk.DrawingArea):
    """Rule-of-thirds grid drawn over the viewfinder."""
    def __init__(self):
        super().__init__()
        self.set_can_target(False)  # transparent to input
        self.set_draw_func(self._draw)

    def _draw(self, area, cr, w, h):
        cr.set_source_rgba(1, 1, 1, 0.35)
        cr.set_line_width(1)
        for i in (1, 2):
            cr.move_to(w * i / 3, 0); cr.line_to(w * i / 3, h); cr.stroke()
            cr.move_to(0, h * i / 3); cr.line_to(w, h * i / 3); cr.stroke()


class LensApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = LensWindow(self)
        win.present()


if __name__ == "__main__":
    sys.exit(LensApp().run(sys.argv))
