"""Tests for the data ingestion pipeline."""

import pytest
from unittest.mock import MagicMock

from app.services.data_ingestion import (
    ingest_hospital_transparency_csv,
    ingest_medicare_physician_fee_schedule,
    ingest_nadac_pharmacy,
)


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.merge = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    return db


def test_ingest_hospital_csv(mock_db):
    csv_content = """code,description,discounted_cash_price,negotiated_aetna
99213,Office visit - established,150.00,180.00
99214,Office visit - detailed,220.00,260.00
"""
    count = ingest_hospital_transparency_csv(
        mock_db, csv_content, "Test Hospital", "https://test.com/prices.csv", "CA"
    )
    assert count > 0
    assert mock_db.commit.called


def test_ingest_medicare_pfs(mock_db):
    csv_content = """HCPCS,DESCRIPTION,NON_FACILITY_NA_PAYMENT,FACILITY_NA_PAYMENT
99213,Office visit - established,95.00,65.00
99214,Office visit - detailed,135.00,95.00
"""
    count = ingest_medicare_physician_fee_schedule(mock_db, csv_content, 2024)
    assert count > 0


def test_ingest_nadac(mock_db):
    csv_content = """NDC,NDC Description,NADAC_Per_Unit
00002323001,Lisinopril 10mg,0.05
00002323002,Metformin 500mg,0.03
"""
    count = ingest_nadac_pharmacy(mock_db, csv_content)
    assert count == 2


def test_ingest_empty_csv(mock_db):
    """Empty CSV should not crash."""
    count = ingest_hospital_transparency_csv(mock_db, "", "Empty", "https://test.com", "CA")
    assert count == 0
