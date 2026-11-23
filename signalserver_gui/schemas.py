"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# Projects / folders -----------------------------------------------------------
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""


class ProjectOut(ProjectIn, ORM):
    id: int
    created_at: datetime
    updated_at: datetime


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: int | None = None
    position: int = 0


class FolderOut(FolderIn, ORM):
    id: int
    project_id: int


class MoveIn(BaseModel):
    folder_id: int | None = None


class LayerOut(ORM):
    id: int
    project_id: int
    folder_id: int | None
    name: str
    kind: str
    filename: str
    feature_count: int
    created_at: datetime
    url: str = ""
    bounds: list[float] | None = None


# Stations -------------------------------------------------------------------
class StationIn(BaseModel):
    project_id: int | None = Field(None, description="Defaults to the 'Default' project")
    folder_id: int | None = None
    name: str = Field(min_length=1, max_length=120)
    latitude: float = Field(ge=-70, le=70, description="Signal-Server supports +/-70 degrees")
    longitude: float = Field(ge=-180, le=180)
    height_m: float = Field(10.0, ge=0, le=10000, description="Antenna height above ground")
    polarization: Literal["vertical", "horizontal"] = "vertical"
    rotation_deg: float = Field(0.0, ge=0, lt=360)
    downtilt_deg: float = Field(0.0, ge=-10, le=90)
    downtilt_direction_deg: float = Field(0.0, ge=0, lt=360)
    notes: str = ""


class StationOut(StationIn, ORM):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime


# Antennas -------------------------------------------------------------------
class AntennaUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    rx_gain_dbd: float | None = None
    description: str | None = None
    gain_dbi: float | None = Field(None, ge=-20, le=60)
    manufacturer: str | None = Field(None, max_length=120)
    frequency_mhz: float | None = Field(None, gt=0)


class AntennaOut(ORM):
    id: int
    name: str
    category: str
    pattern: str
    rx_gain_dbd: float
    description: str
    gain_dbi: float | None = None
    frequency_mhz: float | None = None
    h_beamwidth: float | None = None
    v_beamwidth: float | None = None
    electrical_tilt: float | None = None
    front_to_back: float | None = None
    manufacturer: str = ""
    source_format: str = ""
    created_at: datetime


class GenerateIn(BaseModel):
    name: str = Field("", max_length=120)
    category: str = Field("generated", max_length=50)
    kind: Literal["omni", "sector"] = "sector"
    h_beamwidth: float = Field(65.0, ge=1, le=360)
    v_beamwidth: float = Field(10.0, ge=1, le=180)
    gain_dbi: float | None = Field(None, ge=-20, le=40)
    front_to_back: float = Field(25.0, ge=0, le=60)
    electrical_tilt: float = Field(0.0, ge=-30, le=30)
    side_lobe: float = Field(20.0, ge=3, le=60)
    frequency_mhz: float | None = Field(None, gt=0)
    description: str = ""


# Plots ----------------------------------------------------------------------
ElevationModel = Literal["dem3", "cop30", "cop30_buildings"]


class PlotIn(BaseModel):
    project_id: int | None = Field(None, description="Defaults to the 'Default' project")
    folder_id: int | None = None
    name: str = Field(min_length=3, max_length=120)
    antenna_id: int
    tx_station_id: int
    rx_station_id: int | None = None
    frequency_mhz: float = Field(ge=20, le=100000)
    erp_watts: float = Field(1.0, ge=0, le=10_000_000)
    tx_power_w: float | None = Field(
        None, ge=0, le=10_000_000,
        description="If set, ERP = tx power x antenna gain (dBi - 2.15) - cable loss",
    )
    cable_loss_db: float = Field(0.0, ge=0, le=60)
    radius_km: float = Field(10.0, gt=0, le=300)
    resolution: Literal[300, 600, 1200, 3600] = 1200
    elevation_model: ElevationModel = "dem3"
    rx_height_m: float = Field(1.5, ge=0, le=1000)
    rx_threshold: float | None = None
    propagation_model: int = Field(1, ge=1, le=12)
    propagation_mode: int | None = Field(None, ge=1, le=3)
    terrain_code: int | None = Field(None, ge=1, le=6)
    terrain_dielectric: float | None = Field(None, ge=2, le=80)
    terrain_conductivity: float | None = Field(None, ge=0.0001, le=0.01)
    climate_code: int | None = Field(None, ge=1, le=7)
    reliability: int | None = Field(None, ge=1, le=99)
    confidence: int | None = Field(None, ge=1, le=99)
    ground_clutter_m: float | None = Field(None, ge=0, le=500)
    knife_edge: bool = False
    use_dbm: bool = True
    metric_units: bool = True
    do_p2p_analysis: bool = False
    color_palette: str = "rainbow"
    opacity: float = Field(0.7, ge=0, le=1)

    @model_validator(mode="after")
    def _check(self):
        if self.rx_station_id is not None and self.rx_station_id == self.tx_station_id:
            raise ValueError("Transmitter and receiver stations must be different")
        if self.do_p2p_analysis and self.rx_station_id is None:
            raise ValueError("Path analysis needs a receiver station")
        if self.resolution == 3600 and self.elevation_model == "dem3":
            raise ValueError("3600 ppd needs a 1\" elevation model (Copernicus 30 m)")
        return self


class PlotOut(PlotIn, ORM):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime
    latest_run: "RunSummary | None" = None


# Runs / jobs ----------------------------------------------------------------
class JobOut(ORM):
    id: int
    kind: str
    status: str
    stage: str
    progress: float
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunSummary(ORM):
    id: int
    plot_id: int
    plot_name: str | None = None
    status: str
    stage: str = ""
    progress: float = 0.0
    error: str | None = None
    north: float | None
    south: float | None
    east: float | None
    west: float | None
    created_at: datetime
    finished_at: datetime | None
    files: list[str] = []
    legend: list[dict] = []
    image_url: str | None = None
    values_url: str | None = None
    value_top: int | None = None
    value_step: int | None = None
    opacity: float = 0.7
    units: str = "dBm"


class RunDetail(RunSummary):
    params: dict
    command: list[str]
    log: str
    report: dict | None


class PrefetchIn(BaseModel):
    model: Literal["dem3", "cop30", "cop30_buildings"] = "dem3"
    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def _check(self):
        if self.north <= self.south:
            raise ValueError("north must be greater than south")
        return self


PlotOut.model_rebuild()
