import asyncio

from .commerce import RaiseResult, parse_raise_categories, parse_raise_result
from .readonly import FunPayAuthError, FunPayReadOnlyAdapter


class FunPayRaisingAdapter(FunPayReadOnlyAdapter):
    async def categories(self):
        html = await self._request("GET", f"/users/{self.account.id}/")
        return await asyncio.to_thread(parse_raise_categories, self.home_html, html)

    async def raise_category(self, category, before_post=None):
        # Preflight must confirm that each selected node still belongs to this account/game.
        try:
            current = await self.categories()
        except FunPayAuthError:
            raise
        except Exception:
            return RaiseResult("paused", 60)  # Proven before any write: retry reading later.
        if not any(
            c.game_id == category.game_id and set(category.nodes) <= set(c.nodes) for c in current
        ):
            return RaiseResult("rejected")
        if not category.nodes or before_post is None or not await before_post():
            return RaiseResult("paused")
        try:
            text = await self._request(
                "POST",
                "/lots/raise",
                data={
                    "game_id": category.game_id,
                    "node_id": category.nodes[0],
                    "node_ids[]": list(category.nodes),
                    "csrf_token": self._csrf,
                },
            )
            return parse_raise_result(text)
        except FunPayAuthError:
            return RaiseResult("auth")
        except Exception:
            return RaiseResult("unknown")  # Never blindly repeat a potentially accepted POST.
