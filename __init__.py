from anki.hooks import addHook
from aqt import mw
from aqt.utils import showInfo
from aqt.qt import *
import time
import threading
from datetime import datetime, timedelta
import sys, os
import re
import warnings

ADDON_DIR = os.path.dirname(__file__)

sys.path.append(os.path.join(os.path.dirname(__file__), "pypresence"))

try:
    from .pypresence import Presence
    PYPRESENCE_AVAILABLE = True
except ImportError:
    PYPRESENCE_AVAILABLE = False

# Modern Anki exposes per-card reviewer hooks via gui_hooks. The legacy
# addHook("showAnswer", ...) is deprecated and does NOT fire on recent versions,
# which is why per-card updates were missing.
try:
    from aqt import gui_hooks
    GUI_HOOKS_AVAILABLE = True
except Exception:
    GUI_HOOKS_AVAILABLE = False

# Rich presence configuration
CLIENT_ID = '583084701510533126'

# Discord limits "state" and "details" to 128 characters. Stay just under it.
DISCORD_FIELD_MAX = 128


def _clean_text(text):
    """Strip HTML/markup from a card's content and collapse whitespace."""
    if not text:
        return ""
    try:
        # Drop style/script blocks entirely, then any remaining tags.
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        # Decode the most common HTML entities.
        replacements = {
            "&nbsp;": " ", "&amp;": "&", "&lt;": "<",
            "&gt;": ">", "&quot;": '"', "&#39;": "'",
        }
        for entity, char in replacements.items():
            text = text.replace(entity, char)
        text = re.sub(r"\s+", " ", text).strip()
    except Exception:
        return ""
    return text


def _truncate(text, max_len=DISCORD_FIELD_MAX):
    """Cut text to Discord's max field length, adding an ellipsis if needed."""
    if text and len(text) > max_len:
        return text[:max_len - 1].rstrip() + "…"
    return text


class DiscordRichPresence:
    def __init__(self):
        self.rpc = None
        self.connected = False
        self.start_time = int(time.time())
        self.last_update_time = 0
        self.update_cooldown = 2  # Minimum seconds between updates
        self.connection_attempts = 0
        self.max_connection_attempts = 3
        self.retry_delay = 5  # seconds
        self.last_connection_attempt = 0
        self.retry_timer = None  # track the pending retry timer to avoid stacking

        # State tracking
        self.current_state = ""
        self.current_details = ""
        self.due_message = ""
        self.cards_done_today = 0

        # Skip counters for performance
        self.skip_edit = 0
        self.skip_answer = 0

        # Initialize connection
        if PYPRESENCE_AVAILABLE:
            self._connect_with_retry()

    def _schedule_retry(self, delay):
        """Schedule a single background retry, cancelling any previous one."""
        try:
            if self.retry_timer is not None:
                self.retry_timer.cancel()
        except Exception:
            pass

        timer = threading.Timer(delay, self._connect_with_retry)
        timer.daemon = True
        self.retry_timer = timer
        timer.start()

    def _connect_with_retry(self):
        """Attempt to connect to Discord with retry logic"""
        current_time = time.time()

        # Don't retry too frequently
        if current_time - self.last_connection_attempt < self.retry_delay:
            return False

        if self.connection_attempts >= self.max_connection_attempts:
            return False

        self.last_connection_attempt = current_time

        try:
            if self.rpc:
                try:
                    self.rpc.close()
                except Exception:
                    pass

            self.rpc = Presence(CLIENT_ID)
            self.rpc.connect()
            self.connected = True
            self.connection_attempts = 0
            return True

        except Exception:
            self.connected = False
            self.connection_attempts += 1

            # Schedule retry in background
            if self.connection_attempts < self.max_connection_attempts:
                self._schedule_retry(self.retry_delay)

            return False

    def _calculate_cards_done_today(self):
        """Calculate how many cards were reviewed today"""
        if not mw.col:
            return 0

        try:
            anki_today_start = int((mw.col.sched.day_cutoff - 86400) * 1000)

            reviews_today = mw.col.db.scalar(
                "SELECT COUNT(*) FROM revlog WHERE id > ?",
                anki_today_start
            ) or 0

            return reviews_today
        except Exception:
            return 0

    def _calculate_due_cards(self):
        """Calculate cards due and update due message"""
        if not mw.col:
            self.due_message = "Loading..."
            return

        try:
            due_count = 0

            # Loop through deckDueTree to find cards due
            for deck_info in mw.col.sched.deckDueTree():
                name, did, due, lrn, new, children = deck_info
                due_count += due + lrn + new

            # Update cards done today
            self.cards_done_today = self._calculate_cards_done_today()

            # Format the due message
            if due_count == 0:
                self.due_message = f"Done for today! ({self.cards_done_today} cards completed)"
            elif due_count == 1:
                self.due_message = f"1 card left ({self.cards_done_today} done today)"
            else:
                self.due_message = f"{due_count} cards left ({self.cards_done_today} done today)"

        except Exception:
            self.due_message = "Error calculating cards"

    def get_current_card_text(self):
        """Return the cleaned, truncated front text of the card being reviewed."""
        try:
            if mw.state != "review":
                return ""
            reviewer = getattr(mw, "reviewer", None)
            card = getattr(reviewer, "card", None) if reviewer else None
            if not card:
                return ""

            front = _clean_text(card.question())
            if not front:
                return ""

            prefix = "Reviewing: "
            # Reserve room for the prefix so the whole field stays under the limit.
            front = _truncate(front, DISCORD_FIELD_MAX - len(prefix))
            return prefix + front
        except Exception:
            return ""

    def update_presence(self, state, details, small_image="tick-dark", force_update=False):
        """Update Discord Rich Presence with rate limiting and error handling"""
        current_time = time.time()

        # Rate limiting - don't update too frequently unless forced
        if not force_update and current_time - self.last_update_time < self.update_cooldown:
            return

        # Don't update if nothing changed
        if not force_update and state == self.current_state and details == self.current_details:
            return

        # Update card counts
        self._calculate_due_cards()

        # Keep both fields within Discord's length limit
        state = _truncate(state)
        details = _truncate(details)

        # Try to connect if not connected
        if not self.connected:
            if not self._connect_with_retry():
                return

        try:
            activity = {
                "state": state or self.due_message,
                "details": details,
                "start": self.start_time,
                "small_image": small_image,
                "small_text": "Anki Flashcards",
                "large_image": "anki_final",
                "large_text": "Anki - Spaced Repetition"
            }

            self.rpc.update(**activity)

            # Update tracking variables
            self.current_state = state
            self.current_details = details
            self.last_update_time = current_time

        except Exception:
            self.connected = False

            # Try to reconnect in background
            self._schedule_retry(1.0)

    def clear(self):
        """Clear the presence shown in Discord without dropping the connection."""
        if self.rpc and self.connected:
            try:
                self.rpc.clear()
                self.current_state = ""
                self.current_details = ""
            except Exception:
                pass

    def close(self):
        """Clean up Discord connection"""
        try:
            if self.retry_timer is not None:
                self.retry_timer.cancel()
                self.retry_timer = None
        except Exception:
            pass

        if self.rpc and self.connected:
            try:
                self.rpc.clear()
            except Exception:
                pass
            try:
                self.rpc.close()
            except Exception:
                pass
        self.connected = False


# Global instance
try:
    discord_rpc = DiscordRichPresence()
except Exception:
    discord_rpc = None


def on_state_change(state, old_state):
    """Handle Anki state changes"""
    global discord_rpc

    if not PYPRESENCE_AVAILABLE or not discord_rpc:
        return

    try:
        # Map states to Discord presence
        if state == "overview":
            # Overview = a deck is finished or selected. Previously this returned
            # early, which left the presence stuck on "Daily reviews" forever.
            discord_rpc.update_presence(
                discord_rpc.due_message,
                "Chilling in the menus",
                "zzz"
            )
        elif state == "deckBrowser":
            discord_rpc.update_presence(
                discord_rpc.due_message,
                "Chilling in the menus",
                "zzz"
            )
        elif state == "review":
            details = discord_rpc.get_current_card_text() or "Daily reviews"
            discord_rpc.update_presence(
                discord_rpc.due_message,
                details,
                "tick-dark"
            )
        elif state == "browse":
            discord_rpc.skip_edit = 1
            discord_rpc.update_presence(
                discord_rpc.due_message,
                "Browsing decks",
                "search"
            )
        elif state == "edit":
            discord_rpc.update_presence(
                discord_rpc.due_message,
                "Adding cards",
                "ellipsis-dark"
            )

    except Exception:
        pass


def on_browse_opened(browser):
    """Handle browser window opening"""
    if not discord_rpc:
        return
    discord_rpc.update_presence(
        discord_rpc.due_message,
        "Browsing decks",
        "search"
    )


def on_editor_opened(editor, note=None):
    """Handle editor opening"""
    global discord_rpc
    if not discord_rpc:
        return

    # Skip if we just opened browser
    if discord_rpc.skip_edit == 0:
        discord_rpc.update_presence(
            discord_rpc.due_message,
            "Adding cards",
            "ellipsis-dark"
        )

    discord_rpc.skip_edit = 0


def on_review_card(card=None):
    """Fired for every card shown in the reviewer (modern gui_hook).

    Runs on each question/answer, so it also covers moving to the next card
    or switching decks while still inside review - the afterStateChange hook
    does NOT fire in those cases.
    """
    global discord_rpc
    if not discord_rpc:
        return

    details = discord_rpc.get_current_card_text() or "Daily reviews"
    discord_rpc.update_presence(
        discord_rpc.due_message,
        details,
        "tick-dark"
    )


def on_answer_shown():
    """Legacy fallback for old Anki versions without gui_hooks."""
    on_review_card()


def on_collection_loaded():
    """Handle collection being loaded - good time to update counts"""
    if not discord_rpc:
        return
    discord_rpc.update_presence(
        discord_rpc.due_message,
        "Chilling in the menus",
        "zzz",
        force_update=True
    )


def cleanup_on_close():
    """Clean up when Anki closes"""
    if discord_rpc:
        discord_rpc.close()


# Register hooks
if PYPRESENCE_AVAILABLE and discord_rpc:
    addHook("afterStateChange", on_state_change)
    addHook("browser.setupMenus", on_browse_opened)
    addHook("setupEditorShortcuts", on_editor_opened)
    addHook("profileLoaded", on_collection_loaded)
    addHook("unloadProfile", cleanup_on_close)

    # Per-card updates while reviewing. Prefer modern gui_hooks (fires on every
    # card); fall back to the legacy showAnswer hook on very old Anki versions.
    if GUI_HOOKS_AVAILABLE:
        gui_hooks.reviewer_did_show_question.append(on_review_card)
        gui_hooks.reviewer_did_show_answer.append(on_review_card)
    else:
        addHook("showAnswer", on_answer_shown)

    # Ensure cleanup on exit
    import atexit
    atexit.register(cleanup_on_close)
