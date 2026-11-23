"""SQLAlchemy models (PostgreSQL + PostGIS).

All lengths are stored in metric units (metres / kilometres); the engine
command builder converts when a plot asks for imperial output.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import Point, box
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow, onupdate=utcnow
    )


ELEVATION_MODELS = ("dem3", "cop30", "cop30_buildings")


class Project(TimestampMixin, Base):
    """A workspace holding a tree of folders with stations, plots and imported layers."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class Folder(TimestampMixin, Base):
    __tablename__ = "folders"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    position: Mapped[int] = mapped_column(Integer, default=0)


class Station(TimestampMixin, Base):
    __tablename__ = "stations"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    location: Mapped[object] = mapped_column(Geometry("POINT", srid=4326, spatial_index=True))
    height_m: Mapped[float] = mapped_column(Float, default=10.0)
    polarization: Mapped[str] = mapped_column(String(10), default="vertical")
    rotation_deg: Mapped[float] = mapped_column(Float, default=0.0)
    downtilt_deg: Mapped[float] = mapped_column(Float, default=0.0)
    downtilt_direction_deg: Mapped[float] = mapped_column(Float, default=0.0)
    notes: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        CheckConstraint("polarization in ('vertical','horizontal')", name="station_polarization"),
        CheckConstraint("height_m >= 0", name="station_height_positive"),
        UniqueConstraint("project_id", "name", name="uq_station_project_name"),
    )

    @property
    def latitude(self) -> float:
        return to_shape(self.location).y

    @property
    def longitude(self) -> float:
        return to_shape(self.location).x

    def set_position(self, latitude: float, longitude: float) -> None:
        self.location = from_shape(Point(longitude, latitude), srid=4326)


class Antenna(TimestampMixin, Base):
    __tablename__ = "antennas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    category: Mapped[str] = mapped_column(String(50), default="custom")
    pattern: Mapped[str] = mapped_column(String(120))  # file base name under antennas/<category>/
    rx_gain_dbd: Mapped[float] = mapped_column(Float, default=0.0)
    description: Mapped[str] = mapped_column(Text, default="")


class Plot(TimestampMixin, Base):
    __tablename__ = "plots"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    antenna_id: Mapped[int] = mapped_column(ForeignKey("antennas.id", ondelete="RESTRICT"))
    # NO ACTION (checked at end of statement) so deleting a project/folder can cascade
    # through plots and their stations together; a direct station delete is still refused.
    tx_station_id: Mapped[int] = mapped_column(ForeignKey("stations.id"))
    rx_station_id: Mapped[int | None] = mapped_column(ForeignKey("stations.id"), nullable=True)
    elevation_model: Mapped[str] = mapped_column(String(20), default="dem3")

    frequency_mhz: Mapped[float] = mapped_column(Float)
    erp_watts: Mapped[float] = mapped_column(Float, default=1.0)
    radius_km: Mapped[float] = mapped_column(Float, default=10.0)
    resolution: Mapped[int] = mapped_column(Integer, default=1200)
    rx_height_m: Mapped[float] = mapped_column(Float, default=1.5)
    rx_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)

    propagation_model: Mapped[int] = mapped_column(Integer, default=1)
    propagation_mode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terrain_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    terrain_dielectric: Mapped[float | None] = mapped_column(Float, nullable=True)
    terrain_conductivity: Mapped[float | None] = mapped_column(Float, nullable=True)
    climate_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reliability: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ground_clutter_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    knife_edge: Mapped[bool] = mapped_column(Boolean, default=False)

    use_dbm: Mapped[bool] = mapped_column(Boolean, default=True)
    metric_units: Mapped[bool] = mapped_column(Boolean, default=True)
    do_p2p_analysis: Mapped[bool] = mapped_column(Boolean, default=False)
    color_palette: Mapped[str] = mapped_column(String(40), default="rainbow")
    opacity: Mapped[float] = mapped_column(Float, default=0.7)

    antenna: Mapped[Antenna] = relationship(lazy="joined")
    tx_station: Mapped[Station] = relationship(foreign_keys=[tx_station_id], lazy="joined")
    rx_station: Mapped[Station | None] = relationship(foreign_keys=[rx_station_id], lazy="joined")
    runs: Mapped[list["PlotRun"]] = relationship(
        back_populates="plot", cascade="all, delete-orphan", passive_deletes=True,
        order_by="PlotRun.id.desc()",
    )

    __table_args__ = (
        CheckConstraint("rx_station_id is null or rx_station_id <> tx_station_id", name="plot_distinct_stations"),
        CheckConstraint("frequency_mhz >= 20 and frequency_mhz <= 100000", name="plot_frequency_range"),
        CheckConstraint("radius_km > 0 and radius_km <= 300", name="plot_radius_range"),
        CheckConstraint(
            "resolution in (300, 600, 1200) or (resolution = 3600 and elevation_model <> 'dem3')",
            name="plot_resolution",
        ),
        CheckConstraint("opacity >= 0 and opacity <= 1", name="plot_opacity"),
        CheckConstraint(
            "elevation_model in ('dem3','cop30','cop30_buildings')", name="plot_elevation_model"
        ),
        UniqueConstraint("project_id", "name", name="uq_plot_project_name"),
    )


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"


class Job(Base):
    """Generic background job, claimed by workers with ``FOR UPDATE SKIP LOCKED``."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), default=JobStatus.queued
    )
    stage: Mapped[str] = mapped_column(String(80), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_jobs_status_id", "status", "id"),)


class PlotRun(Base):
    """One execution of a plot: its inputs snapshot, outputs and footprint."""

    __tablename__ = "plot_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    plot_id: Mapped[int] = mapped_column(ForeignKey("plots.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    command: Mapped[list] = mapped_column(JSONB, default=list)
    log: Mapped[str] = mapped_column(Text, default="")
    north: Mapped[float | None] = mapped_column(Float, nullable=True)
    south: Mapped[float | None] = mapped_column(Float, nullable=True)
    east: Mapped[float | None] = mapped_column(Float, nullable=True)
    west: Mapped[float | None] = mapped_column(Float, nullable=True)
    footprint: Mapped[object | None] = mapped_column(
        Geometry("POLYGON", srid=4326, spatial_index=True), nullable=True
    )
    files: Mapped[list] = mapped_column(JSONB, default=list)
    legend: Mapped[list] = mapped_column(JSONB, default=list)
    # Per-pixel values: values.png holds index i -> value_top - i*value_step (255 = no data).
    value_top: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value_unit: Mapped[str | None] = mapped_column(String(10), nullable=True)
    report: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    plot: Mapped[Plot] = relationship(back_populates="runs")
    job: Mapped[Job | None] = relationship(lazy="joined")

    def set_bounds(self, north: float, east: float, south: float, west: float) -> None:
        self.north, self.east, self.south, self.west = north, east, south, west
        self.footprint = from_shape(box(west, south, east, north), srid=4326)


class ElevationTile(Base):
    """A 1x1 degree DEM tile prepared for Signal-Server."""

    __tablename__ = "elevation_tiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(String(20), default="dem3")
    lat: Mapped[int] = mapped_column(Integer)
    lon: Mapped[int] = mapped_column(Integer)
    building_count: Mapped[int] = mapped_column(Integer, default=0)
    has_data: Mapped[bool] = mapped_column(Boolean)  # False: no source tile (sea level)
    source: Mapped[str] = mapped_column(String(80), default="")
    min_elevation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_elevation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    footprint: Mapped[object] = mapped_column(Geometry("POLYGON", srid=4326, spatial_index=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow)

    __table_args__ = (UniqueConstraint("model", "lat", "lon", name="uq_elevation_tile"),)


class ElevationArchive(Base):
    """A Viewfinder Panoramas zip that has been downloaded and unpacked."""

    __tablename__ = "elevation_archives"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    url: Mapped[str] = mapped_column(String(300))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    tile_count: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow)


class Layer(Base):
    """An imported vector layer (KML/KMZ) shown on the globe."""

    __tablename__ = "layers"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("folders.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(10), default="kml")
    filename: Mapped[str] = mapped_column(String(200))
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    footprint: Mapped[object | None] = mapped_column(
        Geometry("POLYGON", srid=4326, spatial_index=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow)


class BasemapPack(Base):
    """An offline street-map extract (PMTiles) for a region."""

    __tablename__ = "basemap_packs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    west: Mapped[float] = mapped_column(Float)
    south: Mapped[float] = mapped_column(Float)
    east: Mapped[float] = mapped_column(Float)
    north: Mapped[float] = mapped_column(Float)
    max_zoom: Mapped[int] = mapped_column(Integer, default=14)
    source: Mapped[str] = mapped_column(String(200), default="")
    tile_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), default=utcnow)
