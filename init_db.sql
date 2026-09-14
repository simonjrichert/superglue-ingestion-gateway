CREATE TABLE legacy_customers (
    id SERIAL PRIMARY KEY,
    raw_name VARCHAR(255),
    email VARCHAR(255),
    account_status VARCHAR(50),
    credit_card VARCHAR(100),
    created_at VARCHAR(100)
);

INSERT INTO legacy_customers (raw_name, email, account_status, credit_card, created_at) VALUES
('Acme Corp', 'contact@acme.com', 'ACTIVE', '4111-2222-3333-4444', '2026-01-15'),
('Globex Inc', 'invalid-email-format', 'active', '4555-6666-7777-8888', '2026-02-01'),
('Initech LLC', 'support@initech.io', 'PENDING', '4999-0000-1111-2222', '2026-02-10'),
('Stark Ind', 'tony@stark.com', 'UNKNOWN_STATUS', '4333-2222-1111-0000', 'invalid-date'),
('Umbrella Corp', 'info@umbrella.com', 'INACTIVE', '4000-1234-5678-9010', '2026-03-01');
