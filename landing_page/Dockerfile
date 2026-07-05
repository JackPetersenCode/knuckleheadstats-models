FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY index.html style.css kh_logo.png record.json record.csv /usr/share/nginx/html/
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
