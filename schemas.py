from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SalesforceAccount(BaseModel):
    """Salesforce Account contract this example pipeline is allowed to emit."""

    model_config = ConfigDict(extra="forbid")

    Name: str = Field(min_length=1)
    AccountExternalId__c: str
    Type: Literal["Customer"]
    Status__c: Literal["Active", "Inactive"]
    CreatedDate: date


class SalesforceContact(BaseModel):
    """Salesforce Contact linked to Account via the same external id."""

    model_config = ConfigDict(extra="forbid")

    LastName: str = Field(min_length=1)
    Email: EmailStr
    AccountExternalId__c: str


OBJECT_MODELS = {
    "Account": SalesforceAccount,
    "Contact": SalesforceContact,
}
