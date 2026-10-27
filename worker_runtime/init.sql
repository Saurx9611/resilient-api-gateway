CREATE ROLE gateway LOGIN PASSWORD 'dev-only-local';
CREATE DATABASE gateway_dev OWNER gateway;
CREATE DATABASE gateway_test OWNER gateway;
