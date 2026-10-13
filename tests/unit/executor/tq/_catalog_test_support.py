"""天勤目录只提供代码；规格由实际订阅的行情对象提供。"""

import asyncio
import re


class CatalogApiMixin:
    futures = ["SHFE.rb2610"]
    options = []
    combines = []

    def query_quotes(self, *, ins_class=None, expired=False, exchange_id=None, product_id=None):
        self.query_count = getattr(self, "query_count", 0) + 1
        if expired:
            return []
        kinds = {"FUTURE": self.futures, "OPTION": self.options, "COMBINE": self.combines}
        rows = (
            [s for kind in ins_class for s in kinds[kind]]
            if isinstance(ins_class, list)
            else kinds.get(ins_class, self.futures + self.options + self.combines)
        )
        if exchange_id:
            rows = [s for s in rows if s.startswith(exchange_id + ".")]
        if product_id:
            products = product_id if isinstance(product_id, list) else [product_id]
            rows = [s for s in rows if re.search(r"[A-Za-z_]+(?=\d)", s.split(".", 1)[-1]).group() in products]
        return rows

    def get_position(self):
        return {}

    def get_order(self):
        return {}

    def create_task(self, coroutine):
        if not hasattr(self, "_catalog_loop"):
            self._catalog_loop = asyncio.new_event_loop()
        return self._catalog_loop.create_task(coroutine)

    def pump_catalog(self):
        if hasattr(self, "_catalog_loop"):
            self._catalog_loop.run_until_complete(asyncio.sleep(0))

    def close_catalog(self):
        if hasattr(self, "_catalog_loop"):
            self._catalog_loop.close()
