from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from mto_bi import dataset, sample

AS_OF = date(2026, 10, 4)


@pytest.fixture(scope="session")
def demo_sample():
    return sample.generate(AS_OF)


@pytest.fixture(scope="session")
def demo_files(demo_sample):
    return sample.files(demo_sample)


@pytest.fixture(scope="session")
def demo(demo_files):
    return dataset.build(dataset.classify(demo_files))


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s)
