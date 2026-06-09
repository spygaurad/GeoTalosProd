# Setup

## Local Development

```bash
cp .env.docker .env
docker compose up --build
```

## Services Started

- API
- PostgreSQL + PostGIS
- pgSTAC
- TiTiler
- Redis
- MinIO
- Celery workers
- Celery Beat
- Martin
- Flower

## More Details

For deeper deployment and infrastructure notes, see:

- [Infrastructure README](../infra/README.md)
