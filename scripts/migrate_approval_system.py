"""
Standalone migration runner for Purchase Orders, Segregation of Duties,
and Encumbrance Accounting.
Can be executed directly: python scripts/migrate_approval_system.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def run_migration():
    from app.extensions import db
    # We can create a lightweight connection
    from app import create_app
    app = create_app()

    with app.app_context():
        engine = db.engine
        dialect_name = engine.dialect.name
        print(f"Applying Purchase Order schema updates to {dialect_name} database...")

        with engine.connect() as conn:
            # 1. Add approval_limit column to users if not present
            if dialect_name == "postgresql":
                conn.execute(db.text("""
                    DO $$ 
                    BEGIN 
                        IF NOT EXISTS (
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name='users' AND column_name='approval_limit'
                        ) THEN 
                            ALTER TABLE users ADD COLUMN approval_limit DOUBLE PRECISION DEFAULT 0.0 NOT NULL;
                        END IF;
                        ALTER TABLE users ALTER COLUMN role TYPE VARCHAR(30);
                    END $$;
                """))
            else:
                # SQLite
                try:
                    conn.execute(db.text("ALTER TABLE users ADD COLUMN approval_limit FLOAT DEFAULT 0.0 NOT NULL;"))
                except Exception:
                    pass

            # 2. Create tables using SQLAlchemy metadata
            from app.models.purchasing import PurchaseOrder, POLine, POAuditTrail
            PurchaseOrder.__table__.create(bind=engine, checkfirst=True)
            POLine.__table__.create(bind=engine, checkfirst=True)
            POAuditTrail.__table__.create(bind=engine, checkfirst=True)

            conn.commit()

        print("[OK] Purchase Orders, PO Lines, Audit Trails & User Approval Limits applied successfully!")

if __name__ == "__main__":
    run_migration()
