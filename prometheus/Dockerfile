FROM prom/prometheus:v3.15.0

COPY --chmod=0644 prometheus.yml /etc/prometheus/prometheus.yml
COPY --chmod=0644 alerts.yml /etc/prometheus/alerts.yml

RUN /bin/promtool check config /etc/prometheus/prometheus.yml
