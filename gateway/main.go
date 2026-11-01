package main

import (
"encoding/json"
"fmt"
"html/template"
"log"
"net/http"
"os"
"time"
)

type MeResponse struct {
Connector struct {
ID         int64  `json:"id"`
ProviderID string `json:"provider_id"`
NodeName   string `json:"node_name"`
Status     string `json:"status"`
Address    string `json:"address"`
DNSName    string `json:"dns_name"`
} `json:"connector"`

User struct {
ID       int64  `json:"id"`
Username string `json:"username"`
} `json:"user"`
}

type PageData struct {
User      string
NodeName  string
Connector string
}

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
    <p>Gateway: {{.NodeName}}</p>

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

func fetchIdentity(
client *http.Client,
apiURL string,
providerID string,
) (*MeResponse, error) {
req, err := http.NewRequest(
http.MethodGet,
apiURL+"/v1/me",
nil,
)
if err != nil {
return nil, err
}

req.Header.Set("X-AI-Connector-ID", providerID)

resp, err := client.Do(req)
if err != nil {
return nil, err
}
defer resp.Body.Close()

if resp.StatusCode != http.StatusOK {
return nil, fmt.Errorf(
"gateway API returned %s",
resp.Status,
)
}

var result MeResponse

if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
return nil, err
}

return &result, nil
}

func main() {
providerID := os.Getenv("AI_CONNECTOR_PROVIDER_ID")
if providerID == "" {
log.Fatal("AI_CONNECTOR_PROVIDER_ID is required")
}

apiURL := os.Getenv("AI_GATEWAY_API_URL")
if apiURL == "" {
apiURL = "http://gateway-api:8080"
}

client := &http.Client{
Timeout: 5 * time.Second,
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

me, err := fetchIdentity(
client,
apiURL,
providerID,
)
if err != nil {
log.Printf("identity lookup failed: %v", err)
http.Error(
w,
"Platform service unavailable",
http.StatusServiceUnavailable,
)
return
}

w.Header().Set(
"Content-Type",
"text/html; charset=utf-8",
)

err = page.Execute(w, PageData{
User:      me.User.Username,
NodeName:  me.Connector.NodeName,
Connector: me.Connector.ProviderID,
})
if err != nil {
log.Printf("template error: %v", err)
}
})

log.Printf(
"gateway connector %s listening on 127.0.0.1:8080",
providerID,
)

if err := http.ListenAndServe("127.0.0.1:8080", nil); err != nil {
log.Fatal(err)
}
}
