"""Max exposure: the user can set it anywhere up to 15% of equity (RISK.md override 2026-11-02), never above."""
import pytest
from fastapi import HTTPException

from app.api.settings_routes import RiskUpdateRequest, update_risk


def test_max_exposure_takes_up_to_15_percent_and_no_more(db_session):
    assert update_risk(RiskUpdateRequest(max_total_exposure=0.15), session=db_session).risk.max_total_exposure == 0.15
    with pytest.raises(HTTPException) as err:
        update_risk(RiskUpdateRequest(max_total_exposure=0.16), session=db_session)
    assert err.value.status_code == 400 and "0.15" in err.value.detail
    with pytest.raises(HTTPException):
        update_risk(RiskUpdateRequest(max_total_exposure=0), session=db_session)
    # lowering it is always allowed
    assert update_risk(RiskUpdateRequest(max_total_exposure=0.06), session=db_session).risk.max_total_exposure == 0.06
