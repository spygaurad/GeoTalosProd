from fastapi import Query


DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def limit_param(limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)) -> int:
    return limit


def offset_param(offset: int = Query(0, ge=0)) -> int:
    return offset
