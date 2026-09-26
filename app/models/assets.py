import uuid
from datetime import date

from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin

ASSET_CATEGORIES = {
    "Office Equipment": "1500",
    "Vehicles": "1510",
    "Furniture & Fittings": "1520",
    "IT Equipment": "1530",
    "Land & Buildings": "1540",
}

ASSET_STATUSES = ["In Use", "Under Repair", "Disposed"]


class FixedAsset(TenantScopedMixin, db.Model):
    __tablename__ = "fixed_assets"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    serial_number = db.Column(db.String(100))
    custodian = db.Column(db.String(150))
    location = db.Column(db.String(150))
    project_id = db.Column(UUID(as_uuid=True), db.ForeignKey("projects.id"), nullable=True)
    acquisition_date = db.Column(db.Date, nullable=False, default=date.today)
    cost = db.Column(db.Float, nullable=False, default=0.0)
    salvage_value = db.Column(db.Float, default=0.0)
    useful_life_years = db.Column(db.Integer, default=5)
    status = db.Column(db.String(20), default="In Use")
    disposal_date = db.Column(db.Date)
    disposal_value = db.Column(db.Float)
    last_depreciated_on = db.Column(db.Date)
    acquisition_entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=True)

    project = db.relationship("Project")

    def accumulated_depreciation(self, as_of=None):
        as_of = as_of or date.today()
        if self.status == "Disposed" and self.disposal_date:
            as_of = min(as_of, self.disposal_date)
        depreciable = max((self.cost or 0) - (self.salvage_value or 0), 0)
        if self.useful_life_years <= 0 or depreciable <= 0:
            return 0.0
        years_elapsed = (as_of - self.acquisition_date).days / 365.25
        years_elapsed = max(0, min(years_elapsed, self.useful_life_years))
        annual_dep = depreciable / self.useful_life_years
        return round(annual_dep * years_elapsed, 2)

    def net_book_value(self, as_of=None):
        return round((self.cost or 0) - self.accumulated_depreciation(as_of), 2)

    @property
    def annual_depreciation(self):
        depreciable = max((self.cost or 0) - (self.salvage_value or 0), 0)
        if self.useful_life_years <= 0:
            return 0.0
        return round(depreciable / self.useful_life_years, 2)
