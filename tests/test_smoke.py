"""Offline smoke tests: python -m unittest discover -s tests -v"""
import asyncio
import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from logic import parse_offer_args, miniapp_link, offer_text, price_label, status_label
from security import authenticated_user_id, InvalidInitData
from storage import Store


class OfferParsingTests(unittest.TestCase):
    def test_short_offer_format(self):
        link, title, reference, price, currency = parse_offer_args(
            ['https://t.me/nft/ViceCream-279721', '900', 'stars'])
        self.assertEqual(link, 'https://t.me/nft/ViceCream-279721')
        self.assertEqual(title, 'Vice Cream #279721')
        self.assertEqual((reference, price, currency), ('', '900', 'STARS'))

    def test_ton_format(self):
        self.assertEqual(parse_offer_args(['t.me/nft/MoodPack-81539', '1.5', 'ton'])[-2:],
                         ('1.5', 'TON'))

    def test_bad_price_rejected(self):
        for p in ('0', '-2', 'NaN', '1.3'):
            with self.subTest(price=p), self.assertRaises(ValueError):
                parse_offer_args(['t.me/nft/MoodPack-81539', p, 'stars'])

    def test_old_four_arg_format_rejected(self):
        with self.assertRaises(ValueError):
            parse_offer_args(['t.me/nft/MoodPack-81539', 'Mood', '900', 'stars'])

    def test_transfer_label_and_link(self):
        self.assertEqual(status_label('TRANSFER_CONFIRMED'), 'Передача подтверждена')
        self.assertIn('?startapp=abc', miniapp_link('GiftSendRelayerBot', 'abc'))


class AuthenticationTests(unittest.TestCase):
    TOKEN = '12345:TEST_TOKEN_NOT_REAL'

    def signed(self, include_signature=False, sign_signature=False):
        params = {'user': json.dumps({'id': 4321}, separators=(',', ':')),
                  'auth_date': str(int(time.time())), 'query_id': 'test-query'}
        if include_signature:
            params['signature'] = 'arbitrary-third-party-signature'
        check = dict(params)
        if include_signature and not sign_signature:
            check.pop('signature')
        text = '\n'.join(f'{k}={v}' for k, v in sorted(check.items()))
        key = hmac.new(b'WebAppData', self.TOKEN.encode(), hashlib.sha256).digest()
        params['hash'] = hmac.new(key, text.encode(), hashlib.sha256).hexdigest()
        return urlencode(params)

    def test_valid_signature_standard_and_with_third_party_field(self):
        for data in (self.signed(), self.signed(True), self.signed(True, True)):
            self.assertEqual(authenticated_user_id(data, self.TOKEN), 4321)

    def test_wrong_bot_rejected(self):
        with self.assertRaises(InvalidInitData):
            authenticated_user_id(self.signed(), self.TOKEN+'wrong')

    def test_tampered_user_rejected(self):
        with self.assertRaises(InvalidInitData):
            authenticated_user_id(self.signed().replace('%22id%22%3A4321','%22id%22%3A8888'), self.TOKEN)

    def test_missing_auth_rejected(self):
        with self.assertRaises(InvalidInitData):
            authenticated_user_id('', self.TOKEN)


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'giftrelayer_forward.sqlite3'
        self.store = Store(self.path)
        await self.store.open()

    async def asyncTearDown(self):
        await self.store.close()
        self.directory.cleanup()

    async def new_offer(self):
        return await self.store.create(
            buyer_id=8919897939, item_title='Mood Pack #81539',
            gift_url='https://t.me/nft/MoodPack-81539',
            amount='900', currency='STARS', duration_hours=6,
            buyer_username='buyername', buyer_first_name='Buyer',
        )

    async def test_offer_accept_and_profile(self):
        offer, secret = await self.new_offer()
        self.assertIsNotNone(await self.store.get_by_secret(secret))
        self.assertEqual((await self.store.get(offer.id)).buyer_username, 'buyername')
        self.assertEqual((await self.store.act(offer.id, 8919897939, 'ACCEPTED'))[0], 'FORBIDDEN')
        ok, changed = await self.store.act(offer.id, 1234567, 'ACCEPTED')
        self.assertEqual((ok, changed.status, changed.seller_id), ('OK', 'ACCEPTED', 1234567))
        self.assertEqual((await self.store.act(offer.id, 999, 'ACCEPTED'))[0], 'ALREADY_DONE')

    async def test_inline_feedback_records_multiple_messages(self):
        offer, _ = await self.new_offer()
        self.assertTrue(await self.store.add_inline_message(offer.id, 'inline-message-A'))
        self.assertTrue(await self.store.add_inline_message(offer.id, 'inline-message-B'))
        await self.store.add_inline_message(offer.id, 'inline-message-A')
        self.assertEqual(set(await self.store.list_inline_messages(offer.id)),
                         {'inline-message-A', 'inline-message-B'})
        self.assertFalse(await self.store.add_inline_message('missing-offer', 'inline-message-C'))

    async def test_migrate_previous_db_without_data_loss(self):
        await self.store.close()
        con = sqlite3.connect(self.path)
        con.execute('ALTER TABLE offers DROP COLUMN buyer_username')
        con.execute('ALTER TABLE offers DROP COLUMN buyer_first_name')
        con.commit()
        con.close()
        await self.store.open()
        offer, _ = await self.new_offer()
        self.assertEqual((await self.store.get(offer.id)).buyer_username, 'buyername')
        self.assertEqual(await self.store.list_inline_messages(offer.id), [])


if __name__ == '__main__':
    unittest.main()
