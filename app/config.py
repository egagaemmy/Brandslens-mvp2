"""Config — every external dependency is optional except Claude. Anything
unset just means that collector quietly does nothing until you add the key;
it never blocks the rest of the app from running."""
import os

def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

APP_NAME = "BrandsLens"
TIMEZONE = env("TZ", "Africa/Lagos")
FRONTEND_ORIGIN = env("FRONTEND_ORIGIN", "*")
# The blog's own canonical address — a dedicated subdomain of the real
# domain, since the blog is served by this backend directly, not by the
# marketing site (Vercel) or the app (Cloudflare Pages). Used for
# canonical/OG tags and social share links, which all need the blog's
# own real, public URL — not the marketing site's.
BLOG_URL = env("BLOG_URL", "https://blog.brandslens.com")

# --- Claude (classification + statement drafting) — the one API this needs ---
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
CLASSIFIER_MODEL = env("CLASSIFIER_MODEL", "claude-haiku-4-5")

# --- Free / no-approval collectors ---
YOUTUBE_API_KEY = env("YOUTUBE_API_KEY")                # console.cloud.google.com, instant key, generous free quota
REDDIT_CLIENT_ID = env("REDDIT_CLIENT_ID")               # reddit.com/prefs/apps, instant, free
REDDIT_CLIENT_SECRET = env("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = env("REDDIT_USER_AGENT", "brandseye-mvp/0.1")
TELEGRAM_API_ID = env("TELEGRAM_API_ID")                 # my.telegram.org, instant, free
TELEGRAM_API_HASH = env("TELEGRAM_API_HASH")
TELEGRAM_SESSION = env("TELEGRAM_SESSION", "brandseye_mvp")
TIPLINE_BOT_TOKEN = env("TIPLINE_BOT_TOKEN")             # from @BotFather, instant, free
WHOISXML_API_KEY = env("WHOISXML_API_KEY")               # optional — dnstwist+certstream work without it

# --- X/Twitter — OFF by default. Flip on the day it's funded; nothing else changes. ---
X_ENABLED = env("X_ENABLED", "false").lower() == "true"
X_BEARER_TOKEN = env("X_BEARER_TOKEN")

# --- Alerts ---
RESEND_API_KEY = env("RESEND_API_KEY")
# Points at whatever email marketing platform you choose — Mailchimp,
# ConvertKit, Brevo, etc. all accept an incoming webhook or a Zapier/Make
# relay URL here. Leave unset and signups still land safely in our own
# database; nothing is ever lost either way.
NEWSLETTER_WEBHOOK_URL = env("NEWSLETTER_WEBHOOK_URL")
# Mailchimp needs real authentication and its own specific request format —
# a generic webhook POST alone was never going to work against Mailchimp's
# actual API. If these three are set, they take priority over the generic
# webhook above. All three come from your Mailchimp account: the API key
# from Account > Extras > API keys (its suffix, e.g. "-us21", is the
# server prefix), and the list/audience ID from Audience > Settings.
MAILCHIMP_API_KEY = env("MAILCHIMP_API_KEY")
MAILCHIMP_SERVER_PREFIX = env("MAILCHIMP_SERVER_PREFIX")
MAILCHIMP_LIST_ID = env("MAILCHIMP_LIST_ID")
ADMIN_SETUP_SECRET = env("ADMIN_SETUP_SECRET")  # temporary — protects the one-time /api/setup/create-admin route
MAIL_FROM = env("MAIL_FROM", "watch@mail.brandslens.com")
ENTERPRISE_INQUIRY_EMAIL = env("ENTERPRISE_INQUIRY_EMAIL", "kgrnigeria@gmail.com")
SLACK_WEBHOOK_DEFAULT = env("SLACK_WEBHOOK_DEFAULT")

# --- Billing — optional. Organizations simply stay 'trialing' until these exist. ---
STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET")
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY")

# --- Cadence (minutes) ---
CADENCE_NEWS = int(env("CADENCE_NEWS", "20"))
CADENCE_NAIRALAND = int(env("CADENCE_NAIRALAND", "45"))
CADENCE_HACKERNEWS = int(env("CADENCE_HACKERNEWS", "20"))
CADENCE_REDDIT = int(env("CADENCE_REDDIT", "20"))
CADENCE_YOUTUBE = int(env("CADENCE_YOUTUBE", "60"))
CADENCE_DOMAINS = int(env("CADENCE_DOMAINS", "1440"))
CADENCE_TELEGRAM_FLUSH = int(env("CADENCE_TELEGRAM_FLUSH", "5"))
CADENCE_X = int(env("CADENCE_X", "15"))
CADENCE_SLA_SWEEP = int(env("CADENCE_SLA_SWEEP", "5"))
