# Stage 1: build heaptool
FROM rust:latest AS rust-builder
WORKDIR /build
RUN git clone https://github.com/TeamMonumenta/monumenta-automation.git
WORKDIR /build/monumenta-automation/rust
RUN cargo build --release --bin heaptool

# Stage 2: Python runtime
FROM python:3.12-slim
COPY --from=rust-builder /build/monumenta-automation/rust/target/release/heaptool /usr/local/bin/heaptool
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
EXPOSE 8081
CMD ["python", "server.py"]
