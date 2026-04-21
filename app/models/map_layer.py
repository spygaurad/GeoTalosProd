import uuid
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


_SOURCE_CHECK = """
(source_type = 'dataset_item'    AND (dataset_item_id IS NOT NULL OR stac_item_id IS NOT NULL)) OR
(source_type = 'stac_item'       AND stac_item_id IS NOT NULL) OR
(source_type = 'dataset_mosaic'  AND dataset_id IS NOT NULL) OR
(source_type = 'dataset'         AND dataset_id IS NOT NULL) OR
(source_type = 'stac_search'     AND source_config ? 'searchid') OR
(source_type = 'annotation_set'  AND annotation_set_id IS NOT NULL) OR
(source_type = 'feature_layer'   AND feature_layer_id IS NOT NULL) OR
(source_type = 'tile_source'     AND tile_source_id IS NOT NULL) OR
(source_type = 'tile_service'    AND (tile_source_id IS NOT NULL OR tile_service_url IS NOT NULL)) OR
(source_type = 'basemap'         AND basemap_id IS NOT NULL) OR
(source_type = 'xarray_variable' AND source_config ? 'variable_ref')
"""


class MapLayer(Base):
    __tablename__ = "map_layers"
    __table_args__ = (
        Index("idx_map_layers_map", "map_id"),
        Index("idx_map_layers_dataset", "dataset_id"),
        Index("idx_map_layers_dataset_item", "dataset_item_id"),
        Index("idx_map_layers_feature_layer", "feature_layer_id"),
        Index("idx_map_layers_basemap", "basemap_id"),
        Index("idx_map_layers_style", "style_id"),
        Index("idx_map_layers_annotation_set", "annotation_set_id"),
        CheckConstraint(_SOURCE_CHECK, name="ck_map_layers_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    map_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("maps.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    layer_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=True
    )
    dataset_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dataset_items.id", ondelete="CASCADE"), nullable=True
    )
    stac_item_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tile_service_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    tile_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tile_sources.id", ondelete="SET NULL"), nullable=True
    )
    annotation_set_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("annotation_sets.id", ondelete="CASCADE"), nullable=True
    )
    feature_layer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("feature_layers.id", ondelete="CASCADE"), nullable=True
    )
    basemap_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("basemaps.id", ondelete="CASCADE"), nullable=True
    )
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("styles.id", ondelete="SET NULL"), nullable=True
    )
    style_override: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # render_config — unified bag for raster rendering params the resolver passes
    # to titiler: {rescale, colormap_name, expression, nodata, assets, band_index}.
    render_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # filter_config — per-layer attribute filter applied by resolver to vector
    # fetches (e.g. {confidence: {gte: 0.7}}, {class_id: {in: [...]}}).
    filter_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # aoi_filter — per-layer AOI overriding Map.aoi_geometry for this layer.
    aoi_filter: Mapped[object | None] = mapped_column(
        Geometry("GEOMETRY", srid=4326, spatial_index=False), nullable=True
    )
    time_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    z_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    visible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    opacity: Mapped[float] = mapped_column(Float, nullable=False, server_default="1.0")
    min_zoom: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_zoom: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    map: Mapped["Map"] = relationship("Map", back_populates="layers")
    dataset: Mapped["Dataset | None"] = relationship("Dataset", back_populates="map_layers")
    dataset_item: Mapped["DatasetItem | None"] = relationship("DatasetItem")
    tile_source: Mapped["TileSource | None"] = relationship("TileSource")
    annotation_set: Mapped["AnnotationSet | None"] = relationship("AnnotationSet")
    feature_layer: Mapped["FeatureLayer | None"] = relationship("FeatureLayer")
    basemap: Mapped["Basemap | None"] = relationship("Basemap")
    style: Mapped["Style | None"] = relationship("Style")