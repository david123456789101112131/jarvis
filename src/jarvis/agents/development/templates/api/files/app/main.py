"""The API: health check and CRUD for items. Run with: python -m uvicorn app.main:app --port 8000"""

from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, load_settings
from app.models import Item, ItemCreate, ItemPage, ItemUpdate
from app.storage import JsonStore


def create_app(settings: Settings | None = None, store: JsonStore | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = store or JsonStore(settings.data_file)
    app = FastAPI(title=settings.title, description=settings.description, version="1.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.store = store

    @app.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        return {"status": "ok", "items": len(store.list())}

    @app.get("/items", response_model=ItemPage, tags=["items"])
    def list_items(
        q: Annotated[str, Query(max_length=100, description="Text to search in name and description")] = "",
        tag: Annotated[str, Query(max_length=30)] = "",
        done: bool | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ItemPage:
        items = store.list()
        if q:
            needle = q.lower()
            items = [i for i in items if needle in i["name"].lower() or needle in i["description"].lower()]
        if tag:
            items = [i for i in items if tag.lower() in i["tags"]]
        if done is not None:
            items = [i for i in items if i["done"] is done]
        page = items[offset: offset + limit]
        return ItemPage(items=[Item(**i) for i in page], total=len(items), limit=limit, offset=offset)

    @app.get("/items/{item_id}", response_model=Item, tags=["items"])
    def get_item(item_id: int) -> Item:
        item = store.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return Item(**item)

    @app.post("/items", response_model=Item, status_code=status.HTTP_201_CREATED, tags=["items"])
    def create_item(body: ItemCreate) -> Item:
        return Item(**store.create(body.model_dump()))

    @app.put("/items/{item_id}", response_model=Item, tags=["items"])
    def replace_item(item_id: int, body: ItemCreate) -> Item:
        item = store.update(item_id, body.model_dump())
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return Item(**item)

    @app.patch("/items/{item_id}", response_model=Item, tags=["items"])
    def update_item(item_id: int, body: ItemUpdate) -> Item:
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        item = store.update(item_id, changes) if changes else store.get(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return Item(**item)

    @app.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["items"])
    def delete_item(item_id: int) -> Response:
        if not store.delete(item_id):
            raise HTTPException(status_code=404, detail="Item not found")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


app = create_app()
