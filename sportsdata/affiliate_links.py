"""Affiliate link config for the daily value post.

Replace the placeholder URLs with your real affiliate/referral links once your
programs are approved (most require an LLC + EIN; apply via the books' affiliate
portals or networks like impact.com / myaffiliates). The daily post auto-inserts
these so every recommended book routes to your link.

Until a real link is set, the post shows the book name with no link.
"""

# book key (as stored in odds tables) -> your affiliate URL ("" = not set yet)
AFFILIATE_LINKS = {
    "draftkings": "",   # e.g. "https://dkng.co/yourid"
    "fanduel":    "",
    "betmgm":     "",
    "caesars":    "",
    "espnbet":    "",
    "sleeper":    "",
}

# pretty display names for posts
BOOK_NAMES = {
    "draftkings": "DraftKings", "fanduel": "FanDuel", "betmgm": "BetMGM",
    "caesars": "Caesars", "espnbet": "ESPN BET", "sleeper": "Sleeper",
}


def link(book):
    return AFFILIATE_LINKS.get(book, "") or ""


def name(book):
    return BOOK_NAMES.get(book, book.title())
