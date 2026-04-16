"""GraphQL tools for direct structured queries against the Fabric GraphQL API.

These tools complement the MCP data agents by providing precise, filterable,
and aggregation-capable queries against the AdventureWorks GraphQL endpoint.
"""

from __future__ import annotations

import json
import os
from typing import Annotated, Optional

import httpx
from agent_framework import tool
from pydantic import Field


# ---------------------------------------------------------------------------
# Shared GraphQL client
# ---------------------------------------------------------------------------

async def _execute_graphql(
    query: str,
    variables: dict | None = None,
    headers: dict | None = None,
) -> dict:
    """Execute a GraphQL query against the Fabric GraphQL API endpoint."""
    url = os.environ["FABRIC_GRAPHQL_API_URL"]

    # Headers are injected at call time so the refreshed Fabric token is used
    request_headers = {
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)

    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=request_headers)
        resp.raise_for_status()
        body = resp.json()

    if "errors" in body:
        return {"errors": body["errors"]}
    return body.get("data", body)


def _fmt(data: dict) -> str:
    """Return compact JSON for the agent to interpret."""
    return json.dumps(data, indent=2, default=str)


# ---------------------------------------------------------------------------
# Tool 1 — Search Products
# ---------------------------------------------------------------------------

@tool(
    name="search_products",
    description=(
        "Search the product catalog with optional filters on color, price range, "
        "size, category, and product model. Returns structured product data. "
        "Use this when the user asks for specific product lookups or filtered "
        "product lists."
    ),
)
async def search_products(
    color: Annotated[Optional[str], Field(description="Filter by product color (e.g. 'Red', 'Black')")] = None,
    min_price: Annotated[Optional[float], Field(description="Minimum list price")] = None,
    max_price: Annotated[Optional[float], Field(description="Maximum list price")] = None,
    size: Annotated[Optional[str], Field(description="Filter by product size (e.g. 'S', 'M', 'L')")] = None,
    category_id: Annotated[Optional[int], Field(description="Filter by ProductCategoryID")] = None,
    product_number: Annotated[Optional[str], Field(description="Filter by product number (contains match)")] = None,
    limit: Annotated[int, Field(description="Max results to return (default 20)")] = 20,
    **kwargs,
) -> str:
    from .agent import fabric_headers, refresh_fabric_headers
    refresh_fabric_headers()

    # Build filter clauses
    filters = []
    if color:
        filters.append(f'Color: {{ eq: "{color}" }}')
    if min_price is not None:
        filters.append(f"ListPrice: {{ gte: {min_price} }}")
    if max_price is not None:
        filters.append(f"ListPrice: {{ lte: {max_price} }}")
    if size:
        filters.append(f'Size: {{ eq: "{size}" }}')
    if category_id is not None:
        filters.append(f"ProductCategoryID: {{ eq: {category_id} }}")
    if product_number:
        filters.append(f'ProductNumber: {{ contains: "{product_number}" }}')

    filter_arg = ""
    if filters:
        filter_arg = f', filter: {{ {", ".join(filters)} }}'

    query = f"""
    query {{
      products(first: {limit}{filter_arg}, orderBy: {{ ListPrice: DESC }}) {{
        items {{
          ProductID
          ProductNumber
          Color
          StandardCost
          ListPrice
          Size
          Weight
          ProductCategoryID
          ProductModelID
          SellStartDate
          SellEndDate
          DiscontinuedDate
        }}
        hasNextPage
      }}
    }}
    """
    data = await _execute_graphql(query, headers=fabric_headers)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Tool 2 — Get Customer Info
# ---------------------------------------------------------------------------

@tool(
    name="get_customer_info",
    description=(
        "Look up customer details by customer ID, email address, or company name. "
        "Returns customer profile data. Use for precise customer lookups."
    ),
)
async def get_customer_info(
    customer_id: Annotated[Optional[int], Field(description="Filter by exact CustomerID")] = None,
    email: Annotated[Optional[str], Field(description="Filter by email address (contains match)")] = None,
    company_name: Annotated[Optional[str], Field(description="Filter by company name (contains match)")] = None,
    limit: Annotated[int, Field(description="Max results to return (default 10)")] = 10,
    **kwargs,
) -> str:
    from .agent import fabric_headers, refresh_fabric_headers
    refresh_fabric_headers()

    filters = []
    if customer_id is not None:
        filters.append(f"CustomerID: {{ eq: {customer_id} }}")
    if email:
        filters.append(f'EmailAddress: {{ contains: "{email}" }}')
    if company_name:
        filters.append(f'CompanyName: {{ contains: "{company_name}" }}')

    filter_arg = ""
    if filters:
        filter_arg = f', filter: {{ {", ".join(filters)} }}'

    query = f"""
    query {{
      customers(first: {limit}{filter_arg}) {{
        items {{
          CustomerID
          Title
          Suffix
          CompanyName
          SalesPerson
          EmailAddress
          rowguid
          ModifiedDate
        }}
        hasNextPage
      }}
    }}
    """
    data = await _execute_graphql(query, headers=fabric_headers)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Tool 3 — Get Customer Addresses
# ---------------------------------------------------------------------------

@tool(
    name="get_customer_addresses",
    description=(
        "Retrieve all addresses linked to a given customer. Joins through the "
        "CustomerAddress mapping to return full address details. Use after "
        "looking up a customer to find their shipping/billing addresses."
    ),
)
async def get_customer_addresses(
    customer_id: Annotated[int, Field(description="The CustomerID to retrieve addresses for")],
    **kwargs,
) -> str:
    from .agent import fabric_headers, refresh_fabric_headers
    refresh_fabric_headers()

    # Step 1: get AddressIDs for the customer
    mapping_query = f"""
    query {{
      customerAddresses(filter: {{ CustomerID: {{ eq: {customer_id} }} }}) {{
        items {{
          CustomerID
          AddressID
        }}
      }}
    }}
    """
    mapping_data = await _execute_graphql(mapping_query, headers=fabric_headers)

    if "errors" in mapping_data:
        return _fmt(mapping_data)

    address_ids = [
        item["AddressID"]
        for item in mapping_data.get("customerAddresses", {}).get("items", [])
    ]

    if not address_ids:
        return _fmt({"message": f"No addresses found for CustomerID {customer_id}"})

    # Step 2: get full address details
    ids_list = ", ".join(str(a) for a in address_ids)
    address_query = f"""
    query {{
      addresses(filter: {{ AddressID: {{ in: [{ids_list}] }} }}) {{
        items {{
          AddressID
          AddressLine1
          AddressLine2
          City
          PostalCode
        }}
      }}
    }}
    """
    address_data = await _execute_graphql(address_query, headers=fabric_headers)
    return _fmt({
        "customer_id": customer_id,
        "addresses": address_data.get("addresses", {}).get("items", []),
    })


# ---------------------------------------------------------------------------
# Tool 4 — Get Sales Orders
# ---------------------------------------------------------------------------

@tool(
    name="get_sales_orders",
    description=(
        "Query sales order headers with optional filters on customer, date range, "
        "status, and order totals. Returns order-level summary data. "
        "Use for questions about a customer's orders or orders in a time period."
    ),
)
async def get_sales_orders(
    customer_id: Annotated[Optional[int], Field(description="Filter by CustomerID")] = None,
    order_date_after: Annotated[Optional[str], Field(description="Orders on or after this ISO date (e.g. '2024-01-01')")] = None,
    order_date_before: Annotated[Optional[str], Field(description="Orders on or before this ISO date")] = None,
    min_total: Annotated[Optional[float], Field(description="Minimum SubTotal amount")] = None,
    status: Annotated[Optional[int], Field(description="Filter by order status code")] = None,
    limit: Annotated[int, Field(description="Max results to return (default 25)")] = 25,
    order_by: Annotated[str, Field(description="Field to sort by: OrderDate, SubTotal, SalesOrderID")] = "OrderDate",
    order_dir: Annotated[str, Field(description="Sort direction: ASC or DESC")] = "DESC",
    **kwargs,
) -> str:
    from .agent import fabric_headers, refresh_fabric_headers
    refresh_fabric_headers()

    filters = []
    if customer_id is not None:
        filters.append(f"CustomerID: {{ eq: {customer_id} }}")
    if order_date_after:
        filters.append(f'OrderDate: {{ gte: "{order_date_after}" }}')
    if order_date_before:
        filters.append(f'OrderDate: {{ lte: "{order_date_before}" }}')
    if min_total is not None:
        filters.append(f"SubTotal: {{ gte: {min_total} }}")
    if status is not None:
        filters.append(f"Status: {{ eq: {status} }}")

    filter_arg = ""
    if filters:
        filter_arg = f', filter: {{ {", ".join(filters)} }}'

    query = f"""
    query {{
      salesOrderHeaders(first: {limit}{filter_arg}, orderBy: {{ {order_by}: {order_dir} }}) {{
        items {{
          SalesOrderID
          RevisionNumber
          OrderDate
          DueDate
          ShipDate
          Status
          CustomerID
          ShipToAddressID
          BillToAddressID
          ShipMethod
          SubTotal
          TaxAmt
          Freight
          Comment
        }}
        hasNextPage
      }}
    }}
    """
    data = await _execute_graphql(query, headers=fabric_headers)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Tool 5 — Get Order Line Items
# ---------------------------------------------------------------------------

@tool(
    name="get_order_line_items",
    description=(
        "Retrieve detailed line items for a specific sales order. Shows each "
        "product in the order with quantity, unit price, and discounts. "
        "Use when the user wants to see what's inside a specific order."
    ),
)
async def get_order_line_items(
    sales_order_id: Annotated[int, Field(description="The SalesOrderID to get line items for")],
    **kwargs,
) -> str:
    from .agent import fabric_headers, refresh_fabric_headers
    refresh_fabric_headers()

    query = f"""
    query {{
      salesOrderDetails(filter: {{ SalesOrderID: {{ eq: {sales_order_id} }} }}, orderBy: {{ SalesOrderDetailID: ASC }}) {{
        items {{
          SalesOrderID
          SalesOrderDetailID
          OrderQty
          ProductID
          UnitPrice
          UnitPriceDiscount
        }}
      }}
    }}
    """
    data = await _execute_graphql(query, headers=fabric_headers)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Tool 6 — Sales Analytics / Aggregations
# ---------------------------------------------------------------------------

@tool(
    name="get_sales_analytics",
    description=(
        "Run sales analytics using GraphQL aggregations. Supports grouping orders "
        "by fields like Status, CustomerID, or ShipMethod and computing "
        "sum/avg/min/max/count on numeric fields such as SubTotal, TaxAmt, Freight. "
        "Use for revenue summaries, order counts, and trend analysis."
    ),
)
async def get_sales_analytics(
    group_by_field: Annotated[str, Field(description="Field to group by: Status, CustomerID, ShipMethod, OrderDate, etc.")] = "Status",
    agg_type: Annotated[str, Field(description="Aggregation function: sum, avg, min, max, count")] = "sum",
    agg_field: Annotated[str, Field(description="Numeric field to aggregate: SubTotal, TaxAmt, Freight, SalesOrderID, etc.")] = "SubTotal",
    customer_id: Annotated[Optional[int], Field(description="Optional: filter to a specific customer")] = None,
    order_date_after: Annotated[Optional[str], Field(description="Optional: orders on or after this ISO date")] = None,
    order_date_before: Annotated[Optional[str], Field(description="Optional: orders on or before this ISO date")] = None,
    limit: Annotated[int, Field(description="Max groups to return (default 50)")] = 50,
    **kwargs,
) -> str:
    from .agent import fabric_headers, refresh_fabric_headers
    refresh_fabric_headers()

    filters = []
    if customer_id is not None:
        filters.append(f"CustomerID: {{ eq: {customer_id} }}")
    if order_date_after:
        filters.append(f'OrderDate: {{ gte: "{order_date_after}" }}')
    if order_date_before:
        filters.append(f'OrderDate: {{ lte: "{order_date_before}" }}')

    filter_arg = ""
    if filters:
        filter_arg = f', filter: {{ {", ".join(filters)} }}'

    query = f"""
    query {{
      salesOrderHeaders(first: {limit}{filter_arg}) {{
        items {{
          SalesOrderID
        }}
        groupBy(fields: [{group_by_field}]) {{
          fields {{
            {group_by_field}
          }}
          aggregations {{
            {agg_type}(field: {agg_field})
          }}
        }}
      }}
    }}
    """
    data = await _execute_graphql(query, headers=fabric_headers)
    return _fmt(data)


# ---------------------------------------------------------------------------
# Convenience list for importing
# ---------------------------------------------------------------------------

ALL_GRAPHQL_TOOLS = [
    search_products,
    get_customer_info,
    get_customer_addresses,
    get_sales_orders,
    get_order_line_items,
    get_sales_analytics,
]
