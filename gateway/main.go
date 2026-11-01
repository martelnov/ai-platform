package main

import (
"fmt"
"html/template"
"log"
"net/http"
"os"
)

var page = template.Must(template.New("index").Parse(`<!doctype html>
<html lang="cs">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>AI Platform</title>
</head>
<body>
    <h1>AI Platform</h1>

    <p>Uživatel: <strong>{{.User}}</strong></p>

    <h2>Služby</h2>

    <ul>
        <li>ComfyUI</li>
        <li>Ollama</li>
        <li>ACE-Step</li>
        <li>Soubory</li>
        <li>Rezervace</li>
    </ul>

    <p>Stav datového prostoru bude načten z platformy.</p>
</body>
</html>`))

func main() {
user := os.Getenv("AI_GATEWAY_USER")
if user == "" {
log.Fatal("AI_GATEWAY_USER is required")
}

http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
w.Header().Set("Content-Type", "text/plain; charset=utf-8")
fmt.Fprintln(w, "ok")
})

http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
if r.URL.Path != "/" {
http.NotFound(w, r)
return
}

w.Header().Set("Content-Type", "text/html; charset=utf-8")

if err := page.Execute(w, struct {
User string
}{
User: user,
}); err != nil {
log.Printf("template error: %v", err)
}
})

log.Printf("gateway for %s listening on 127.0.0.1:8080", user)

if err := http.ListenAndServe("127.0.0.1:8080", nil); err != nil {
log.Fatal(err)
}
}
