"""Exercise the shared limiter against the disposable local PostgreSQL server."""
import os
from pathlib import Path
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from security import PostgresLimiter


@unittest.skipUnless(os.getenv('BBOARD_TEST_POSTGRES_PORT'), 'No isolated local PostgreSQL test instance')
class PostgresRateTests(unittest.TestCase):
    def test_shared_atomic_limits_and_expiry(self):
        engine = create_engine(URL.create('postgresql+pg8000', host='127.0.0.1',
            port=int(os.environ['BBOARD_TEST_POSTGRES_PORT']), username=os.environ['BBOARD_TEST_POSTGRES_USER'],
            database='postgres'), connect_args={'timeout': 5}, pool_size=4, max_overflow=0)
        try:
            with engine.begin() as connection:
                connection.execute(text('CREATE SCHEMA IF NOT EXISTS app'))
                connection.execute(text((Path(__file__).resolve().parents[1] / 'migrations/002_ip_rate_limits.sql').read_text()))
                seconds = float(connection.execute(text('SELECT extract(second FROM statement_timestamp())')).scalar_one())
            # Keep this bounded check away from the fixed-minute boundary.
            if seconds > 55:
                time.sleep(61 - seconds)
            key = 'a' * 64
            with engine.begin() as connection:
                connection.execute(text('''INSERT INTO app.ip_rate_limits VALUES
                    (:key, date_trunc('second', statement_timestamp()), 0,
                    date_trunc('minute', statement_timestamp()), 60)'''), {'key': key})
            limiters = [PostgresLimiter(engine) for _ in range(4)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                retries = list(pool.map(lambda i: limiters[i % 4].consume(key), range(12)))
            self.assertTrue(all(1 <= retry <= 60 for retry in retries))
            first = limiters[0].consume_with_status('b' * 64)
            self.assertEqual(first.retry_after, 0)
            self.assertEqual((first.second_remaining, first.minute_remaining), (9, 59))
            second = limiters[1].consume_with_status('b' * 64)
            self.assertEqual(second.minute_remaining, 58)
            self.assertTrue(1 <= second.minute_reset <= 60)
            blocked = limiters[2].consume_with_status(key)
            self.assertEqual(blocked.minute_remaining, 0)
            self.assertIn(f'"minute";r=0;t={blocked.retry_after}', blocked.headers()['RateLimit'])
            with engine.begin() as connection:
                count = connection.execute(text('SELECT minute_count FROM app.ip_rate_limits WHERE ip_hash=:key'), {'key': key}).scalar_one()
                self.assertEqual(count, 61)
                connection.execute(text("UPDATE app.ip_rate_limits SET minute_window=minute_window-interval '2 minutes', second_window=second_window-interval '2 minutes' WHERE ip_hash=:key"), {'key': key})
            self.assertEqual(limiters[1].consume(key), 0)
        finally:
            engine.dispose()
