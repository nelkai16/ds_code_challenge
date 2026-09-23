select s.notification_number from service_request s
join hexagon h ON h.geom = s.geom
where s.geom is NOT NULL
limit 1;