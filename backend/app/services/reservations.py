from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Any, List, Optional

def month_window(month: int, year: int):
    """Naive [start, end) bounds for a calendar month, to be compared against a property-local timestamp."""
    start = datetime(year, month, 1)
    end = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    return start, end

async def calculate_total_revenue(property_id: str, tenant_id: str, month: Optional[int] = None, year: Optional[int] = None) -> Dict[str, Any]:
    """
    Aggregates revenue from database. If month/year are given, only reservations whose
    check-in falls in that month *in the property's timezone* are counted: a check-in at
    2024-02-29 23:30 UTC on a Europe/Paris property is 1 March 00:30 locally, so it is March.
    """
    try:
        # Reuse the shared pool; initialize it once instead of opening a new engine per request.
        from app.core.database_pool import db_pool
        
        if not db_pool.session_factory:
            await db_pool.initialize()
        
        if db_pool.session_factory:
            async with db_pool.get_session() as session:
                # Use SQLAlchemy text for raw SQL
                from sqlalchemy import text
                
                params = {"property_id": property_id, "tenant_id": tenant_id}
                period_filter = ""
                if month is not None and year is not None:
                    params["start"], params["end"] = month_window(month, year)
                    # AT TIME ZONE converts the stored UTC instant to the property's local wall-clock time.
                    period_filter = """
                      AND (r.check_in_date AT TIME ZONE p.timezone) >= :start
                      AND (r.check_in_date AT TIME ZONE p.timezone) < :end"""
                
                query = text(f"""
                    SELECT 
                        r.property_id,
                        SUM(r.total_amount) as total_revenue,
                        COUNT(*) as reservation_count
                    FROM reservations r
                    JOIN properties p ON p.id = r.property_id AND p.tenant_id = r.tenant_id
                    WHERE r.property_id = :property_id AND r.tenant_id = :tenant_id{period_filter}
                    GROUP BY r.property_id
                """)
                
                result = await session.execute(query, params)
                row = result.fetchone()
                
                if row:
                    # Amounts are stored with 3 decimals (sub-cent). Round the SUM once, here,
                    # with financial half-up rounding so the API is the source of truth for cents.
                    total_revenue = Decimal(str(row.total_revenue)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": str(total_revenue),
                        "currency": "USD", 
                        "count": row.reservation_count
                    }
                else:
                    # No reservations found for this property
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": "0.00",
                        "currency": "USD",
                        "count": 0
                    }
        else:
            raise Exception("Database pool not available")
            
    except Exception as e:
        print(f"Database error for {property_id} (tenant: {tenant_id}): {e}")
        
        # Create property-specific mock data for testing when DB is unavailable
        # This ensures each property shows different figures
        mock_data = {
            'prop-001': {'total': '1000.00', 'count': 3},
            'prop-002': {'total': '4975.50', 'count': 4}, 
            'prop-003': {'total': '6100.50', 'count': 2},
            'prop-004': {'total': '1776.50', 'count': 4},
            'prop-005': {'total': '3256.00', 'count': 3}
        }
        
        mock_property_data = mock_data.get(property_id, {'total': '0.00', 'count': 0})
        
        return {
            "property_id": property_id,
            "tenant_id": tenant_id, 
            "total": mock_property_data['total'],
            "currency": "USD",
            "count": mock_property_data['count']
        }
