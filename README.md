# nomad-dados

Arquivos offline do app **NOMAD**, publicados por região. São três tipos:

- **mapa:** mapa detalhado `.pmtiles`;
- **lugares:** busca de lugares `.sqlite`;
- **rotas:** rotas Valhalla 3.9.0 `.tar`.

O app baixa direto daqui (Mapas → Regiões). Não precisa de computador nem de iTunes.

- `catalogo.json` é a lista do que existe para baixar. O app lê esse arquivo.
- Cada arquivo fica numa release `<tipo>-<regiao>`.
  - O arquivo é dividido em partes de 512 MiB, cada uma com SHA-256.
  - Cada release tem um `manifest.json`.
- `.github/workflows/gerar.yml` gera e publica os arquivos.
  - Roda sozinho todo mês para os lugares e a cada 3 meses para os mapas e as rotas.
  - Também dá para rodar à mão:
    ```
    gh workflow run gerar.yml --repo mateusmd1220/nomad-dados -f regioes=argentina,chile -f tipos=mapa,lugares,rotas
    ```
- `regions.json` e `build_places.py` são cópias do repositório do app.
  - Não edite aqui.
  - Para atualizar, rode `scripts/publicar_servidor.ps1` no projeto do app.

## Fontes e licenças

- © colaboradores do [OpenStreetMap](https://www.openstreetmap.org/copyright), sob a licença ODbL. Os mapas, os lugares e as rotas saem desses dados.
- Mapas: [Protomaps](https://protomaps.com) (basemap) a partir do OpenStreetMap.
- Cidades do mundo: [GeoNames](https://www.geonames.org), CC BY 4.0.
- Aeroportos: [OurAirports](https://ourairports.com), domínio público.
- Rotas: geradas com [Valhalla](https://github.com/valhalla/valhalla), licença MIT.
