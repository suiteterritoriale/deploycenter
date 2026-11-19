import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Icon, IconSize, Spinner } from "@gouvfr-lasuite/ui-components";
import { Button, Input, Modal, ModalSize } from "@gouvfr-lasuite/ui-components";
import { errorToString } from "@/features/api/APIError";
import { useOperatorContext } from "@/features/layouts/components/GlobalLayout";
import {
  useDomainRegistrationCheck,
  useDomainRegistrationSuggestions,
  useMutationRegisterDomain,
} from "@/hooks/useQueries";

const PREFIX = "organizations.services.types.domain_registration.modal";

// How long the typing has to pause before the domain is checked.
const CHECK_DELAY_MS = 600;

export type DomainRegistrationModalProps = {
  isOpen: boolean;
  onClose: () => void;
  organizationId: string;
  instanceName: string;
};

/**
 * Registers a domain name for the organization. The backend decides everything:
 * which names can be registered (our rules, the DNS, then the registrar), and the
 * nameservers it will point to. This only asks, shows the verdict and sends.
 */
export const DomainRegistrationModal = (props: DomainRegistrationModalProps) => {
  const { t } = useTranslation();
  const { operatorId } = useOperatorContext();
  const [domain, setDomain] = useState("");
  const [typed, setTyped] = useState("");
  const [address1, setAddress1] = useState("");
  const [zipCode, setZipCode] = useState("");
  const [city, setCity] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // The domain once its request is recorded: the form gives way to a confirmation.
  const [submitted, setSubmitted] = useState<string | null>(null);

  const suggestions = useDomainRegistrationSuggestions(
    operatorId,
    props.organizationId,
    props.isOpen
  );
  const check = useDomainRegistrationCheck(operatorId, props.organizationId, domain);
  const register = useMutationRegisterDomain(operatorId, props.organizationId);

  // The domain is checked once typing pauses, not on every key.
  useEffect(() => {
    const timeout = setTimeout(
      () => setDomain(typed.trim().toLowerCase()),
      CHECK_DELAY_MS
    );
    return () => clearTimeout(timeout);
  }, [typed]);

  // The address proposed is filled in once, so as not to overwrite what is typed.
  const proposed = suggestions.data?.address;
  useEffect(() => {
    if (!proposed) {
      return;
    }
    setAddress1((current) => current || proposed.address1);
    setZipCode((current) => current || proposed.zip_code);
    setCity((current) => current || proposed.city);
  }, [proposed]);

  const verdict = domain === typed.trim().toLowerCase() ? check.data : undefined;
  const isChecking = domain.includes(".") && (check.isFetching || domain !== typed.trim().toLowerCase());
  const canSubmit =
    !!verdict?.registrable &&
    !isChecking &&
    !!address1.trim() &&
    !!zipCode.trim() &&
    !!city.trim() &&
    !register.isPending;

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!canSubmit) {
      return;
    }
    setErrorMessage(null);
    register.mutate(
      {
        domain,
        address1: address1.trim(),
        zip_code: zipCode.trim(),
        city: city.trim(),
      },
      {
        onSuccess: () => setSubmitted(domain),
        onError: (error) => setErrorMessage(errorToString(error)),
      }
    );
  };

  const verdictLine = () => {
    if (!domain.includes(".")) {
      return null;
    }
    if (isChecking) {
      return (
        <span className="dc__domain-registration__check">
          <Spinner size="sm" />
          {t(`${PREFIX}.checking`)}
        </span>
      );
    }
    if (check.isError) {
      return (
        <span className="dc__domain-registration__check dc__domain-registration__check--warning">
          <Icon name="warning" size={IconSize.SMALL} />
          {errorToString(check.error)}
        </span>
      );
    }
    if (!verdict) {
      return null;
    }
    if (!verdict.registrable) {
      return (
        <span className="dc__domain-registration__check dc__domain-registration__check--warning">
          <Icon name="error" size={IconSize.SMALL} />
          {verdict.message}
        </span>
      );
    }
    return (
      <>
        <span className="dc__domain-registration__check dc__domain-registration__check--ok">
          <Icon name="check_circle" size={IconSize.SMALL} />
          {t(`${PREFIX}.available`)}
        </span>
        {verdict.needs_review && (
          <span className="dc__domain-registration__check dc__domain-registration__check--warning">
            <Icon name="schedule" size={IconSize.SMALL} />
            {t(`${PREFIX}.needs_review`)}
          </span>
        )}
      </>
    );
  };

  return (
    <Modal
      size={ModalSize.LARGE}
      title={`${t(`${PREFIX}.title`)} (${props.instanceName})`}
      closeOnEsc={!register.isPending}
      closeOnClickOutside={!register.isPending}
      isOpen={props.isOpen}
      onClose={register.isPending ? () => {} : props.onClose}
      rightActions={
        submitted ? (
          <Button type="button" onClick={props.onClose}>
            {t(`${PREFIX}.done.close`)}
          </Button>
        ) : (
          <>
            <Button
              type="button"
              onClick={props.onClose}
              variant="secondary"
              disabled={register.isPending}
            >
              {t("common.cancel")}
            </Button>
            <Button
              type="submit"
              form="domain-registration-form"
              disabled={!canSubmit}
              icon={register.isPending ? <Spinner /> : undefined}
            >
              {t(`${PREFIX}.submit`)}
            </Button>
          </>
        )
      }
    >
      {submitted ? (
        <div className="dc__service__attribute__modal__content dc__domain-registration__done">
          <Icon name="check_circle" size={IconSize.LARGE} />
          <p className="dc__domain-registration__done__title">
            {t(`${PREFIX}.done.title`, { domain: submitted })}
          </p>
          <p>{t(`${PREFIX}.done.nothing_to_do`)}</p>
          <p>{t(`${PREFIX}.done.follow`)}</p>
        </div>
      ) : (
      <div className="dc__service__attribute__modal__content">
        <p className="dc__service__attribute__modal__content__help">
          {t(`${PREFIX}.description`)}
        </p>
        <form
          id="domain-registration-form"
          className="dc__domain-registration__form"
          onSubmit={handleSubmit}
        >
          {suggestions.data && suggestions.data.domains.length > 0 && (
            <div className="dc__domain-registration__suggestions">
              <span>{t(`${PREFIX}.suggestions`)}</span>
              <div className="dc__domain-registration__suggestions__list">
                {suggestions.data.domains.map((suggestion) => (
                  <Button
                    key={suggestion}
                    type="button"
                    size="small"
                    variant="secondary"
                    onClick={() => setTyped(suggestion)}
                  >
                    {suggestion}
                  </Button>
                ))}
              </div>
            </div>
          )}
          <Input
            label={t(`${PREFIX}.domain`)}
            placeholder={t(`${PREFIX}.domain_placeholder`)}
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
          />
          <div className="dc__domain-registration__checks">{verdictLine()}</div>
          <fieldset className="dc__domain-registration__holder">
            <legend>{t(`${PREFIX}.holder`)}</legend>
            <Input
              label={t(`${PREFIX}.address1`)}
              value={address1}
              onChange={(e) => setAddress1(e.target.value)}
            />
            <div className="dc__domain-registration__holder__city">
              <Input
                label={t(`${PREFIX}.zip_code`)}
                value={zipCode}
                onChange={(e) => setZipCode(e.target.value)}
              />
              <Input
                label={t(`${PREFIX}.city`)}
                value={city}
                onChange={(e) => setCity(e.target.value)}
              />
            </div>
          </fieldset>
          <div className="dc__service__info">
            <Icon name="info" size={IconSize.SMALL} />
            <span>
              {t(`${PREFIX}.terms`, {
                nameservers: (verdict?.nameservers ?? []).join(", "),
              })}
            </span>
          </div>
          {errorMessage && (
            <p className="dc__domain-selector__error">{errorMessage}</p>
          )}
        </form>
      </div>
      )}
    </Modal>
  );
};
