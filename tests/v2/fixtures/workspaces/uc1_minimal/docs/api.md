# Refund API

Endpoint: POST /refunds

Request fields:
- orderId: string
- reason: string

Client setting:
- maxRetries: 2

The maxRetries value is deliberately lower than the terms document and should become a conflict.
