import abc
import enum
import math
import typing

import matplotlib.colors
import mergedeep
import pandas as pd
import pydantic
import structlog

from wraeblast import insights
from wraeblast.filtering import colors
from wraeblast.filtering.parsers.extended import env


logger = structlog.get_logger()

ItemOrCurrencyOverviewType = typing.Union[
    "insights.CurrencyOverview", "insights.ItemOverview"
]

default_options = {
    # Thresholds for categorizing items and currency based on chaos value
    # TODO: Allow thresholds to be functions of current league length
    "thresholds": {
        k: {"quantile": "QU4"}
        for k in (
            "artifacts",
            "base_types",
            "blighted_maps",
            "cluster_jewels",
            "currency",
            "delirium_orbs",
            "essences",
            "divination_cards",
            "fossils",
            "fragments",
            "incubators",
            "skill_gems",
            "maps",
            "oils",
            "prophecies",
            "scarabs",
            "uniques",
            "vials",
            "watchstones",
        )
    },
    "colormaps": {
        "artifacts": {"name": "Curl_20"},
        "base_types": {"name": "Turbid_20_r"},
        "blighted_maps": {"name": "Devon_20"},
        "cluster_jewels": {"name": "Hawaii_20"},
        "currency": {"name": "Curl_20"},
        "delirium_orbs": {"name": "Curl_20"},
        "divination_cards": {"name": "GrayC_20_r"},
        "essences": {"name": "Curl_20"},
        "flasks": {"name": "Magenta_5"},
        "fossils": {"name": "Tokyo_20"},
        "fragments": {"name": "Tokyo_20"},
        "incubators": {"name": "Tokyo_20"},
        "skill_gems": {"name": "Curl_20"},
        "life_flasks": {"name": "Magenta_5"},
        "mana_flasks": {"name": "BluYl_2"},
        "maps_tiered": {"name": "Devon_20", "vmax": 14},
        "maps": {"name": "Devon_20"},
        "oils": {"name": "Curl_20"},
        "prophecies": {"name": "GrayC_20_r"},
        "uniques": {"name": "Viridis_20"},
        "watchstones": {"name": "Curl_20"},
    },
    "whitelist": {},
}


def _to_camel(string: str) -> str:
    split = string.split("_")
    return split[0] + "".join(word.capitalize() for word in split[1:])


class ItemValue(enum.Enum):
    """An enumeration representing tiers of loot based on value."""

    GARBAGE = "garbage"
    VALUABLE = "valuable"
    HIGHLY_VALUABLE = "valuable_high"
    EXTREMELY_VALUABLE = "valuable_extreme"


class ColormapOptions(pydantic.BaseModel):
    name: str
    vmin: float = 1
    vmax: float = math.log(30_000)

    def get_colormap(self) -> matplotlib.colors.Colormap:
        return colors.get_colormap_by_name(self.name)

    def pick(
        self,
        value: float,
        vmax: typing.Optional[float] = None,
        vmin: typing.Optional[float] = None,
        log_scale: bool = True,
    ) -> colors.Color:
        if vmax is None:
            vmax = self.vmax
        if vmin is None:
            vmin = self.vmin
        return env.colormap_pick(self.name, value, vmax, vmin, log_scale)


class ThresholdOptions(abc.ABC):
    @abc.abstractmethod
    def check_visibility(self, row: pd.Series) -> bool:
        ...

    @abc.abstractmethod
    def get_dataframe_query(self, inverted: bool = False) -> str:
        ...

    def get_tiered_results(
        self,
        overview: "insights.EconomyOverview",
        topk: int = 10,
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        df_above_threshold = self.filter_overview(overview)
        df_below_threshold = self.filter_overview(overview, inverted=True)
        return (
            df_above_threshold[:topk],
            df_above_threshold[topk:],
            df_below_threshold,
        )

    def filter_overview(
        self,
        overview: "insights.EconomyOverview",
        inverted: bool = False,
    ) -> pd.DataFrame:
        return overview.df.query(self.get_dataframe_query(inverted=inverted))


class TieredThresholdOptions(ThresholdOptions, pydantic.BaseModel):
    visibility: float = 1
    valuable: float = 5
    highly_valuable: float = 10
    extremely_valuable: float = 20

    def check_visibility(self, row: pd.Series) -> bool:
        return row.chaos_value >= self.visibility

    def get_dataframe_query(self, inverted: bool = False) -> str:
        op = ">=" if not inverted else "<"
        return f"chaos_value {op} {self.visibility}"

    def get_tags(
        self,
        data: typing.Union[pd.DataFrame, pd.Series, float],
        stack_size: int = 1,
    ) -> list[str]:
        tags = []
        if isinstance(data, pd.DataFrame):
            chaos_value = data.chaos_value.max() * stack_size
        elif isinstance(data, pd.Series):
            chaos_value = data.chaos_value * stack_size
        else:
            chaos_value = data * stack_size
        if chaos_value < self.visibility:
            tags.append(ItemValue.GARBAGE.value)
        else:
            for v in ("valuable", "highly_valuable", "extremely_valuable"):
                if chaos_value >= getattr(self, v):
                    tags.append(ItemValue[v.upper()].value)
        return tags


class QuantileThresholdOptions(ThresholdOptions, pydantic.BaseModel):
    quantile: str

    @property
    def quantile_tuple(self) -> tuple[str, int]:
        return insights.get_quantile_tuple(self.quantile)

    def check_visibility(
        self,
        row: pd.Series,
        stack_size: int = 1,
        overview: typing.Optional["insights.EconomyOverview"] = None,
    ) -> bool:
        q_column, q_value = self.quantile_tuple
        if row[q_column] >= q_value:
            return True
        elif overview is None:
            return False
        chaos_value = row.chaos_value * stack_size
        rows = overview.df[overview.df.chaos_value >= chaos_value]
        if not len(rows):
            return False
        return q_value >= rows.iloc[0][q_column]  # type: ignore

    def get_dataframe_query(self, inverted: bool = False) -> str:
        q_column, q_value = self.quantile_tuple
        op = ">=" if not inverted else "<"
        return f"{q_column} {op} {q_value}"

    def get_tags(
        self,
        data: typing.Union[pd.DataFrame, pd.Series, float],
        stack_size: int = 1,
        overview: typing.Optional["insights.EconomyOverview"] = None,
    ) -> list[str]:
        get_row_tags = lambda r: [
            f'Q{r["quartile"]}',
            f'QU{r["quintile"]}',
            f'D{r["decile"]}',
            f'P{r["percentile"]}',
        ]
        if not isinstance(data, (pd.DataFrame, pd.Series)):
            if overview is None:
                raise RuntimeError("overview must be provided")
            return get_row_tags(
                overview.get_quantiles_for_threshold(float(data) * stack_size)
            )
        if overview is None:
            return get_row_tags(data)
        if isinstance(data, pd.DataFrame):
            quantiles = data.groupby(list(insights.quantiles.keys()))
            return [
                p
                for i, q in enumerate(zip(*quantiles.groups.keys()))
                for p in {["Q", "QU", "D", "P"][i] + str(r) for r in q}
            ]
        chaos_value = data.chaos_value * stack_size
        rows = overview.df[overview.df.chaos_value >= chaos_value]
        if not len(rows):
            return get_row_tags(data)
        chaos_values = rows.chaos_value
        return get_row_tags(rows[chaos_values == chaos_values.min()].iloc[0])


ThresholdOptionsType = typing.Union[
    QuantileThresholdOptions,
    TieredThresholdOptions,
]


class ItemFilterPrerenderOptions(pydantic.BaseModel):
    # ctx: "insights.ItemFilterContext"
    thresholds: dict[str, ThresholdOptionsType] = pydantic.Field(
        default_factory=dict,
    )
    colormaps: dict[str, ColormapOptions] = pydantic.Field(
        default_factory=dict,
    )

    @pydantic.validator("colormaps", pre=True, always=True)
    def set_colormaps_vmax(
        cls, v: dict[str, ColormapOptions]
    ) -> dict[str, ColormapOptions]:
        return v

    @classmethod
    def with_defaults(
        cls,
        overrides: typing.Optional[dict[str, typing.Any]] = None,
        ctx: typing.Optional["insights.ItemFilterContext"] = None,
        set_colormap_maximums: bool = True,
    ) -> "ItemFilterPrerenderOptions":
        if overrides is None:
            overrides = {}
        options = ItemFilterPrerenderOptions(
            **mergedeep.merge(default_options, overrides),
        )
        if ctx is not None:
            for (category_name, colormap_options) in options.colormaps.items():
                try:
                    overview: ItemOrCurrencyOverviewType = getattr(
                        ctx, category_name
                    )
                except AttributeError:
                    continue

                if set_colormap_maximums and (
                    "colormaps" not in overrides
                    or category_name not in overrides["colormaps"]
                    or "vmax" not in overrides["colormaps"][category_name]
                ):
                    colormap_options.vmax = overview.max_chaos_value()

        logger.debug("options.initialized")
        return options

    class Config:
        alias_generator = _to_camel
