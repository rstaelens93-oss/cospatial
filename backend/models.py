import uuid
from datetime import datetime, timedelta
from sqlalchemy import Column, String, Integer, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class UserRegistry(Base):
    __tablename__ = "user_registry"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    unique_account_id = Column(String, unique=True, nullable=True, index=True)
    account_role = Column(String, default="user")
    referral_code_used = Column(String, nullable=True, index=True)
    subscription = relationship("SubscriptionState", back_populates="user", uselist=False)

class SubscriptionState(Base):
    __tablename__ = "subscription_state"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("user_registry.id"), unique=True)
    position_tier = Column(String, default="solo_trial")
    billing_cycle = Column(String, default="none")
    is_active = Column(Boolean, default=True)
    access_granted_until = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=7))
    stripe_customer_id = Column(String, unique=True, nullable=True)
    stripe_card_fingerprint = Column(String, unique=False, nullable=True, index=True)
    total_generated_this_month = Column(Integer, default=0)
    user = relationship("UserRegistry", back_populates="subscription")

class WorkspaceRoom(Base):
    __tablename__ = "workspace_rooms"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id = Column(String, ForeignKey("user_registry.id"))
    secure_room_token = Column(String, unique=True, default=lambda: uuid.uuid4().hex)
    max_connection_cap = Column(Integer, default=1)
    current_connection_count = Column(Integer, default=0)

class InfluencerPartnership(Base):
    __tablename__ = "influencer_partnerships"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("user_registry.id"), unique=True)
    influencer_code = Column(String, unique=True, index=True)
    contract_started_at = Column(DateTime, default=datetime.utcnow)
    contract_ends_at = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=5*365))
    is_contract_active = Column(Boolean, default=True)

class RestrictedDiscountRegistry(Base):
    __tablename__ = "restricted_discount_registry"
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    promo_code = Column(String, unique=True, nullable=False, index=True)
    allowed_unique_account_id = Column(String, ForeignKey("user_registry.unique_account_id"), unique=True)
    is_approved_by_admin = Column(Boolean, default=False)
