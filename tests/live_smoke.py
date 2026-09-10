from ldl_mcp.client import LDLClient


def main() -> None:
    client = LDLClient(delay=0.4)
    hit = client.resolve_current("Mantispa styriaca")
    assert hit, "No result for Mantispa styriaca"
    assert hit.get("CombObjID") == 6916, hit
    tax_id = int(hit["CombSpec_TaxObjID"])
    epi = client.epithet(tax_id)
    assert epi.get("OrigCombSciName") == "Raphidia styriaca", epi.get("OrigCombSciName")
    print("LIVE_SMOKE_OK", hit.get("CombGenuToSubs"), hit.get("CombObjID"), tax_id)


if __name__ == "__main__":
    main()
