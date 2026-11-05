"""Version 2: contact audit table, immutable money columns, contacts_filled counter.

Existing rows are not modified. ``donations.donor_email`` already exists from revision 0001
(NULL for every v1 row), so no data migration is needed.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Money fields (SPEC 2.3): a bug in application code must not be able to rewrite them.
# IS DISTINCT FROM so NULL-to-value changes would be caught too; same-value rewrites are fine.
TRIGGER_FUNCTION = """
CREATE FUNCTION donations_reject_money_update() RETURNS trigger AS $$
BEGIN
    IF NEW.amount_minor IS DISTINCT FROM OLD.amount_minor
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.donated_at IS DISTINCT FROM OLD.donated_at
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.external_id IS DISTINCT FROM OLD.external_id THEN
        RAISE EXCEPTION 'money fields of donations are immutable (donation id %)', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.add_column(
        "sync_runs",
        sa.Column("contacts_filled", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_table(
        "donation_contact_changes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("donation_id", sa.BigInteger(), nullable=False),
        sa.Column("field_name", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "field_name IN ('donor_name', 'donor_email', 'donor_address', 'recipient')",
            name="ck_contact_changes_field",
        ),
        sa.ForeignKeyConstraint(["donation_id"], ["donations.id"]),
        sa.ForeignKeyConstraint(["sync_run_id"], ["sync_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contact_changes_donation_id", "donation_contact_changes", ["donation_id"])
    op.execute(TRIGGER_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_donations_money_immutable BEFORE UPDATE ON donations "
        "FOR EACH ROW EXECUTE FUNCTION donations_reject_money_update()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_donations_money_immutable ON donations")
    op.execute("DROP FUNCTION donations_reject_money_update()")
    op.drop_index("ix_contact_changes_donation_id", table_name="donation_contact_changes")
    op.drop_table("donation_contact_changes")
    op.drop_column("sync_runs", "contacts_filled")
