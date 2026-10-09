from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Offer:
    id: str
    buyer_id: int
    seller_id: int               # 0 = recipient is not yet bound
    gift_url: str
    gift_name: str
    gift_number: int
    price_stars: int            # legacy compatibility; meaningful for STARS only
    created_at: int
    expires_at: int
    status: str
    decided_at: int | None
    currency: str = 'STARS'
    amount: str = ''
    item_title: str = ''
    secret_hash: str = ''
    buyer_username: str = ''
    buyer_first_name: str = ''


class Store:
    def __init__(self, database_path: Path):
        self.path = database_path
        self.lock = asyncio.Lock()
        self.conn: sqlite3.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA busy_timeout=5000')
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY,
                buyer_id INTEGER NOT NULL,
                seller_id INTEGER NOT NULL,
                gift_url TEXT NOT NULL,
                gift_name TEXT NOT NULL,
                gift_number INTEGER NOT NULL,
                price_stars INTEGER NOT NULL CHECK(price_stars > 0),
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                decided_at INTEGER,
                currency TEXT NOT NULL DEFAULT 'STARS',
                amount TEXT NOT NULL DEFAULT '',
                item_title TEXT NOT NULL DEFAULT '',
                secret_hash TEXT NOT NULL DEFAULT '',
                buyer_username TEXT NOT NULL DEFAULT '',
                buyer_first_name TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_offers_buyer ON offers(buyer_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS custom_emojis (
                role TEXT PRIMARY KEY,
                emoji_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inline_messages (
                inline_message_id TEXT PRIMARY KEY,
                offer_id TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_inline_messages_offer ON inline_messages(offer_id);
        ''')
        # Migrate the previous forwarding bot's SQLite database without deleting offers.
        existing = {r[1] for r in self.conn.execute('PRAGMA table_info(offers)')}
        for column, ddl in {
            'currency': "TEXT NOT NULL DEFAULT 'STARS'",
            'amount': "TEXT NOT NULL DEFAULT ''",
            'item_title': "TEXT NOT NULL DEFAULT ''",
            'secret_hash': "TEXT NOT NULL DEFAULT ''",
            'buyer_username': "TEXT NOT NULL DEFAULT ''",
            'buyer_first_name': "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in existing:
                self.conn.execute(f'ALTER TABLE offers ADD COLUMN {column} {ddl}')
        self.conn.commit()

    async def close(self) -> None:
        async with self.lock:
            if self.conn is not None:
                self.conn.close()
                self.conn = None

    async def expire(self) -> int:
        async with self.lock:
            assert self.conn is not None
            now = int(time.time())
            cur = self.conn.execute('''UPDATE offers SET status='EXPIRED', decided_at=?
                WHERE status='ACTIVE' AND expires_at<=?''', (now, now))
            self.conn.commit()
            return cur.rowcount

    async def create(self, *, buyer_id: int, item_title: str, gift_url: str,
                     amount: str, currency: str, duration_hours: int,
                     gift_name: str = '', gift_number: int = 0,
                     buyer_username: str = '', buyer_first_name: str = '') -> tuple[Offer, str]:
        if currency not in ('STARS', 'TON'):
            raise ValueError('unsupported currency')
        now = int(time.time())
        secret = secrets.token_urlsafe(32)
        secret_hash = hashlib.sha256(secret.encode()).hexdigest()
        legacy_stars = int(amount) if currency == 'STARS' else 1
        offer = Offer(
            id=secrets.token_urlsafe(14), buyer_id=buyer_id, seller_id=0,
            gift_url=gift_url, gift_name=gift_name, gift_number=gift_number,
            price_stars=legacy_stars, created_at=now,
            expires_at=now + duration_hours * 3600, status='ACTIVE',
            decided_at=None, currency=currency, amount=amount,
            item_title=item_title, secret_hash=secret_hash,
            buyer_username=(buyer_username or '').lstrip('@')[:32],
            buyer_first_name=(buyer_first_name or '')[:128],
        )
        async with self.lock:
            assert self.conn is not None
            self.conn.execute('''INSERT INTO offers
                (id,buyer_id,seller_id,gift_url,gift_name,gift_number,price_stars,
                 created_at,expires_at,status,decided_at,currency,amount,item_title,secret_hash,
                 buyer_username,buyer_first_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (offer.id, offer.buyer_id, offer.seller_id, offer.gift_url,
                 offer.gift_name, offer.gift_number, offer.price_stars,
                 offer.created_at, offer.expires_at, offer.status, None,
                 offer.currency, offer.amount, offer.item_title, offer.secret_hash,
                 offer.buyer_username, offer.buyer_first_name))
            self.conn.commit()
        return offer, secret

    async def save_buyer_profile(self, offer_id: str, buyer_id: int, *,
                                 username: str = '', first_name: str = '') -> bool:
        """Update only the original buyer's identity, never recipient identity."""
        username = (username or '').lstrip('@')[:32]
        first_name = (first_name or '')[:128]
        async with self.lock:
            assert self.conn is not None
            cur = self.conn.execute(
                "UPDATE offers SET buyer_username=CASE WHEN ? != '' THEN ? ELSE buyer_username END, "
                "buyer_first_name=CASE WHEN ? != '' THEN ? ELSE buyer_first_name END "
                "WHERE id=? AND buyer_id=?",
                (username, username, first_name, first_name, offer_id, buyer_id),
            )
            self.conn.commit()
            return bool(cur.rowcount)

    def _to_offer(self, row: sqlite3.Row | None) -> Offer | None:
        return Offer(**dict(row)) if row is not None else None

    async def add_inline_message(self, offer_id: str, inline_message_id: str) -> bool:
        # Telegram inline message ID, not chat message ID.
        if not inline_message_id or len(inline_message_id) > 512:
            return False
        async with self.lock:
            assert self.conn is not None
            exists = self.conn.execute('SELECT 1 FROM offers WHERE id=?', (offer_id,)).fetchone()
            if not exists:
                return False
            self.conn.execute('''INSERT OR IGNORE INTO inline_messages
                (inline_message_id, offer_id, created_at) VALUES (?, ?, ?)''',
                (inline_message_id, offer_id, int(time.time())))
            self.conn.commit()
            return True

    async def list_inline_messages(self, offer_id: str) -> list[str]:
        async with self.lock:
            assert self.conn is not None
            rows = self.conn.execute(
                'SELECT inline_message_id FROM inline_messages WHERE offer_id=?',
                (offer_id,)).fetchall()
            return [r['inline_message_id'] for r in rows]

    async def get(self, offer_id: str) -> Offer | None:
        async with self.lock:
            assert self.conn is not None
            row = self.conn.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone()
            return self._to_offer(row)

    async def get_by_secret(self, key: str) -> Offer | None:
        if not key or len(key) > 200:
            return None
        digest = hashlib.sha256(key.encode()).hexdigest()
        async with self.lock:
            assert self.conn is not None
            row = self.conn.execute('SELECT * FROM offers WHERE secret_hash=?', (digest,)).fetchone()
            return self._to_offer(row)

    async def list_buyer(self, buyer_id: int, limit: int = 10) -> list[Offer]:
        async with self.lock:
            assert self.conn is not None
            rows = self.conn.execute('''SELECT * FROM offers WHERE buyer_id=?
                ORDER BY created_at DESC LIMIT ?''', (buyer_id, limit)).fetchall()
            return [self._to_offer(r) for r in rows]

    async def get_emojis(self) -> dict[str, str]:
        async with self.lock:
            assert self.conn is not None
            return {row['role']: row['emoji_id'] for row in self.conn.execute(
                'SELECT role,emoji_id FROM custom_emojis')}

    async def save_emojis(self, values: dict[str, str]) -> None:
        async with self.lock:
            assert self.conn is not None
            for role, eid in values.items():
                if role not in ('gift', 'price', 'clock') or not eid.isdigit() or len(eid) > 30:
                    raise ValueError('Invalid custom emoji')
                self.conn.execute('INSERT INTO custom_emojis(role,emoji_id) VALUES(?,?) '
                                  'ON CONFLICT(role) DO UPDATE SET emoji_id=excluded.emoji_id',
                                  (role, eid))
            self.conn.commit()

    async def act(self, offer_id: str, actor_id: int, action: str) -> tuple[str, Offer | None]:
        if action not in ('ACCEPTED', 'DECLINED', 'CANCELLED'):
            raise ValueError('invalid action')
        await self.expire()
        async with self.lock:
            assert self.conn is not None
            row = self.conn.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone()
            if not row:
                return 'NOT_FOUND', None
            offer = self._to_offer(row)
            if offer.status != 'ACTIVE':
                return 'ALREADY_DONE', offer
            if action == 'CANCELLED':
                if actor_id != offer.buyer_id:
                    return 'FORBIDDEN', offer
                new_seller_id = offer.seller_id
            else:
                if actor_id == offer.buyer_id or (offer.seller_id not in (0, actor_id)):
                    return 'FORBIDDEN', offer
                # The first recipient to confirm becomes the recipient of this offer.
                new_seller_id = actor_id if offer.seller_id == 0 else offer.seller_id
            now = int(time.time())
            cur = self.conn.execute('''UPDATE offers SET status=?, seller_id=?, decided_at=?
                WHERE id=? AND status='ACTIVE' AND expires_at>?''',
                (action, new_seller_id, now, offer.id, now))
            self.conn.commit()
            if not cur.rowcount:
                return 'ALREADY_DONE', offer
            row = self.conn.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone()
            return 'OK', self._to_offer(row)

    async def confirm_transfer(self, offer_id: str, actor_id: int) -> tuple[str, Offer | None]:
        """Finalize only after independent, positive NFT ownership verification."""
        async with self.lock:
            assert self.conn is not None
            row = self.conn.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone()
            if not row:
                return 'NOT_FOUND', None
            offer = self._to_offer(row)
            if offer.seller_id != actor_id or offer.status != 'ACCEPTED':
                return 'FORBIDDEN', offer
            now = int(time.time())
            cur = self.conn.execute(
                "UPDATE offers SET status='TRANSFER_CONFIRMED', decided_at=? "
                "WHERE id=? AND status='ACCEPTED' AND seller_id=?",
                (now, offer_id, actor_id),
            )
            self.conn.commit()
            if not cur.rowcount:
                return 'ALREADY_DONE', offer
            return 'OK', self._to_offer(self.conn.execute('SELECT * FROM offers WHERE id=?', (offer_id,)).fetchone())
